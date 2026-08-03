"""The native runtime: the existing model<->tool loop, hardened.

This is a faithful extraction of ``ChatService._stream_with_tools`` with four
changes mandated by the plan:

  * Tool calls go through :class:`~app.agents.gateway.tool_gateway.ToolGateway`
    (permission, approval gate, timeout, audit, truncation) instead of the raw
    executor. The ``tool_result`` event now reports the *real* outcome.
  * A :class:`~app.agents.policies.budget_policy.BudgetGuard` caps steps, tool
    calls, replans, wall-clock, and tokens. Crossing a budget ends the turn
    gracefully with ``finish_reason="budget"`` instead of looping forever.
  * Dangerous tools that lack approval emit an ``approval_required`` event and
    return a blocked ``tool_result`` (Phase 0 behavior; Phase 3 adds resume).
  * No raw chain-of-thought is solicited — the system prompt (built by
    ChatService) now asks for structured execution, not narrated reasoning.

ChatService still owns conversation/model resolution, RAG, history trimming,
the pending assistant :class:`~app.models.Message`, and final persistence;
this runtime only owns the loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from app.agents.approval_coordinator import approval_coordinator
from app.agents.gateway.tool_gateway import ToolGateway
from app.agents.policies import BudgetExceeded, BudgetGuard
from app.agents.planning import build_plan, classify_intent
from app.agents.runtime.stage_executor import safe_positive_int
from app.agents.schemas import (
    AgentEvent,
    AgentTurnContext,
    ev_approval_required,
    ev_done,
    ev_error,
    ev_plan_created,
    ev_token,
    ev_tool_call,
    ev_tool_result,
)
from app.models import AgentRun
from app.providers.base import ChatOptions, ProviderError, ToolCallDef
from app.providers.registry import get_provider_for_config
from app.tools.registry_init import get_default_registry

logger = logging.getLogger(__name__)


class NativeChatRuntime:
    """Hardened single-agent tool loop. Implements the :class:`AgentRuntime` protocol."""

    name = "native"

    async def stream_turn(self, ctx: AgentTurnContext) -> AsyncIterator[AgentEvent]:
        db = ctx.db
        cfg = ctx.model_config
        assistant_msg = ctx.assistant_msg
        conversation_id = ctx.conversation.id

        provider = get_provider_for_config(cfg)
        registry = get_default_registry()
        gateway = ToolGateway(
            db,
            conversation_id=conversation_id,
            assistant_message_id=assistant_msg.id,
            run_id=ctx.run_id,
            user=ctx.user,
            registry=registry,
        )
        guard = BudgetGuard()

        # Tools run when the user enabled them AND the model declares capability
        # (or it's the mock provider, which simulates a search step for demos).
        tools_enabled = bool(
            ctx.enable_tools
            and (getattr(cfg, "supports_tools", False) or (cfg.provider or "") == "mock")
        )
        # Advertise only the tools the intent route allows (e.g. search mode =>
        # web_search/http_get only), never the whole registry.
        tool_names = [t.name for t in registry.list()]
        route = ctx.extra.get("route")
        if route is not None:
            from app.agents.intent_router import filter_tool_names
            tool_names = list(filter_tool_names(tool_names, route))
        tool_schemas = registry.openai_schemas(only=tool_names) if tools_enabled else None

        options = ChatOptions(
            temperature=cfg.temperature,
            top_p=cfg.top_p,
            max_tokens=safe_positive_int(cfg.max_tokens, 1024),
            tools=tool_schemas,
            tool_choice="auto",
        )

        # Agent mode: classify intent + publish a short structured plan. The
        # plan is shown to the user (plan_created) instead of raw chain-of-thought.
        if ctx.enable_tools:
            intent = classify_intent(ctx.user_content)
            ctx.extra["intent"] = intent
            plan_summary, plan_steps = build_plan(intent, ctx.user_content)
            yield ev_plan_created(summary=plan_summary, steps=plan_steps)

        working: list[dict[str, Any]] = list(ctx.messages)
        finish_reason = "stop"

        try:
            while True:
                # Cooperative cancel: /api/agent-runs/{id}/cancel sets this event.
                ctl = ctx.extra.get("run_control")
                if ctl is not None and ctl.cancel.is_set():
                    finish_reason = "cancelled"
                    break
                try:
                    guard.enter_step()
                except BudgetExceeded as exc:
                    finish_reason = "budget"
                    logger.info("native run hit budget: %s", exc.reason)
                    break

                accumulated: list[str] = []
                pending_tool_calls: list[ToolCallDef] = []

                try:
                    async for delta in provider.stream_chat(working, options):
                        ctl = ctx.extra.get("run_control")
                        if ctl is not None and ctl.cancel.is_set():
                            finish_reason = "cancelled"
                            break
                        if delta.content:
                            accumulated.append(delta.content)
                            yield ev_token(delta=delta.content)
                        if delta.tool_calls:
                            pending_tool_calls.extend(delta.tool_calls)
                        if delta.finish_reason:
                            finish_reason = delta.finish_reason
                except ProviderError as exc:
                    # Fold this round's partial text before surfacing the error,
                    # so a mid-stream timeout/network failure still preserves
                    # everything generated so far.
                    assistant_msg.content = (assistant_msg.content or "") + "".join(accumulated)
                    logger.exception("provider error in native run: %s", exc)
                    yield ev_error(
                        code=getattr(exc, "code", "provider_error"), message=str(exc)
                    )
                    return

                # Fold this round's streamed text into the assistant message so a
                # mid-loop disconnect still leaves recoverable content.
                assistant_msg.content = (assistant_msg.content or "") + "".join(accumulated)

                # Tool-calling branch: execute via the gateway, append results, re-stream.
                if (
                    tools_enabled
                    and finish_reason == "tool_calls"
                    and pending_tool_calls
                ):
                    assistant_turn = {
                        "role": "assistant",
                        "content": "".join(accumulated) or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": tc.name, "arguments": tc.arguments},
                            }
                            for tc in pending_tool_calls
                        ],
                    }

                    # Emit tool_call events (with the dangerous flag for UI hint).
                    parsed_calls: list[tuple[ToolCallDef, dict[str, Any]]] = []
                    for tc in pending_tool_calls:
                        try:
                            args = json.loads(tc.arguments) if tc.arguments else {}
                            if not isinstance(args, dict):
                                args = {"_value": args}
                        except json.JSONDecodeError:
                            args = {}
                        parsed_calls.append((tc, args))
                        dangerous = _is_dangerous(registry, tc.name)
                        yield ev_tool_call(
                            id=tc.id, name=tc.name, arguments=args, dangerous=dangerous
                        )

                    working.append(assistant_turn)
                    assistant_msg.metadata_ = {
                        **(assistant_msg.metadata_ or {}),
                        "tool_calls": assistant_turn["tool_calls"],
                        "status": "tool_calling",
                    }
                    await db.commit()

                    # Execute each call through the gateway.
                    for tc, args in parsed_calls:
                        try:
                            guard.enter_tool_call()
                        except BudgetExceeded as exc:
                            finish_reason = "budget"
                            logger.info("native run hit tool budget: %s", exc.reason)
                            # Surface an error tool_result so the transcript is coherent.
                            yield ev_tool_result(
                                id=tc.id, name=tc.name, ok=False,
                                result=None, error=f"budget exceeded: {exc.reason}",
                            )
                            continue

                        execution = await gateway.execute(
                            tool_call_id=tc.id,
                            tool_name=tc.name,
                            arguments=args,
                        )

                        # Dangerous tools: pause the live stream for human approval.
                        if execution.status == "needs_approval" and execution.approval_id:
                            yield ev_approval_required(
                                run_id=ctx.run_id,
                                approval_id=execution.approval_id,
                                tool_name=tc.name,
                                summary=_approval_summary(tc.name, args),
                                risk_level=_risk_for(registry, tc.name),
                                arguments_preview=args,
                            )
                            await self._set_run_status(ctx, "waiting_approval")
                            wr = approval_coordinator.register(
                                run_id=ctx.run_id,
                                approval_id=execution.approval_id,
                                tool_name=tc.name,
                            )
                            try:
                                wr = await approval_coordinator.wait(execution.approval_id)
                            except asyncio.TimeoutError:
                                wr.decision = "timed_out"
                            finally:
                                approval_coordinator.release(execution.approval_id)

                            if wr.decision == "cancelled":
                                await self._set_run_status(ctx, "cancelled")
                                raise asyncio.CancelledError()
                            if wr.decision == "approved":
                                await self._set_run_status(ctx, "running")
                                # Re-run through the gateway; it now finds the approved
                                # ToolApproval and executes for real.
                                execution = await gateway.execute(
                                    tool_call_id=tc.id,
                                    tool_name=tc.name,
                                    arguments=args,
                                )
                            elif wr.decision == "rejected":
                                await self._set_run_status(ctx, "running")
                                reason = wr.reason or "rejected by user"
                                yield ev_tool_result(
                                    id=tc.id, name=tc.name, ok=False,
                                    result=None, error=reason,
                                )
                                working.append({
                                    "role": "tool",
                                    "tool_call_id": tc.id,
                                    "name": tc.name,
                                    "content": json.dumps({"error": reason}, ensure_ascii=False),
                                })
                                continue
                            else:  # timed_out
                                await self._set_run_status(ctx, "running")
                                yield ev_tool_result(
                                    id=tc.id, name=tc.name, ok=False,
                                    result=None, error="approval timed out",
                                )
                                working.append({
                                    "role": "tool",
                                    "tool_call_id": tc.id,
                                    "name": tc.name,
                                    "content": json.dumps({"error": "approval timed out"}, ensure_ascii=False),
                                })
                                continue

                        yield ev_tool_result(
                            id=tc.id,
                            name=tc.name,
                            ok=execution.ok,
                            result=execution.result.get("content") if execution.ok and isinstance(execution.result, dict) else execution.result,
                            error=execution.error,
                        )
                        working.append(execution.to_openai_tool_message())

                    # Re-stream with the tool results folded in.
                    finish_reason = "stop"  # reset; the next round decides
                    continue

                # No further tool calls — done.
                break
        except ProviderError as exc:
            yield ev_error(code="provider_error", message=str(exc))
            return
        except Exception as exc:  # noqa: BLE001 — never let the stream die silently
            logger.exception("unexpected error in native run: %s", exc)
            yield ev_error(code="internal", message="Internal error during generation")
            return

        ctx.extra["finish_reason"] = finish_reason
        ctx.extra["budget"] = guard.snapshot()
        yield ev_done(message_id=assistant_msg.id, finish_reason=finish_reason)

    @staticmethod
    async def _set_run_status(ctx: AgentTurnContext, status: str) -> None:
        """Flip the AgentRun lifecycle status (e.g. waiting_approval <-> running)."""
        try:
            run = await ctx.db.get(AgentRun, ctx.run_id)
            if run is not None and run.status not in ("completed", "failed", "cancelled"):
                run.status = status
                await ctx.db.commit()
        except Exception:  # pragma: no cover - status update is best-effort
            logger.warning("failed to set agent_run status=%s", status, exc_info=True)


# --------------------------------------------------------------------------- #
def _safe_get(registry, name: str):
    try:
        return registry.get(name)
    except Exception:  # noqa: BLE001
        return None


def _is_dangerous(registry, name: str) -> bool:
    tool = _safe_get(registry, name)
    return bool(getattr(tool, "dangerous", False))


def _risk_for(registry, name: str) -> str:
    from app.agents.policies import risk_level_for

    tool = _safe_get(registry, name)
    return risk_level_for(tool).value if tool else "medium"


def _approval_summary(tool_name: str, args: dict[str, Any]) -> str:
    from app.agents.policies import risk_summary

    return risk_summary(tool_name, args)
