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
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from app.agents.approval_coordinator import approval_coordinator
from app.agents.continuation import (
    ContinuationBuffer,
    ContinuationPolicy,
    aggregate_usage,
)
from app.agents.db_mutation import db_mutation_scope
from app.agents.gateway.tool_gateway import ToolGateway
from app.agents.policies import BudgetExceeded, BudgetGuard, BudgetLimits
from app.agents.graph import build_single_agent_graph
from app.agents.planning import build_plan, classify_intent, summarize_prefix
from app.agents.context_manager import ContextManager
from app.agents.output_spill import production_spill_writer
from app.agents.runtime.stage_executor import safe_positive_int
from app.agents.token_budget import PromptAdmissionError, calculate_prompt_budget
from app.model_capabilities import capabilities_from_config
from app.agents.schemas import (
    AgentEvent,
    AgentTurnContext,
    ToolExecution,
    ev_agent_graph,
    ev_agent_status,
    ev_approval_required,
    ev_done,
    ev_error,
    ev_plan_created,
    ev_run_paused,
    ev_run_resumed,
    ev_run_status,
    ev_step_completed,
    ev_step_started,
    ev_token,
    ev_tool_call,
    ev_tool_result,
    bounded_json_observation,
)
from app.agents.events import append_event_safe
from app.models import AgentRun
from app.core.config import get_settings
from app.core.pricing import usage_cost
from app.db import AsyncSessionLocal
from app.providers.base import (
    ChatOptions,
    ProviderError,
    ToolCallDef,
    provider_output_token_parameter,
)
from app.providers.registry import get_provider_for_config
from app.tools.registry_init import get_default_registry

logger = logging.getLogger(__name__)

_CONTINUE_INSTRUCTION = (
    "Continue exactly where the answer stopped. Do not repeat any text already given."
)


class NativeChatRuntime:
    """Hardened single-agent tool loop. Implements the :class:`AgentRuntime` protocol."""

    name = "native"

    async def stream_turn(self, ctx: AgentTurnContext) -> AsyncIterator[AgentEvent]:
        """Thin wrapper: guarantees per-tenant connector sessions are torn down
        at run end (graceful shutdown), regardless of how the turn exits. The
        real loop lives in :meth:`_stream_turn_body`; connector sessions opened
        there are stashed on ``ctx.extra`` and closed here."""
        try:
            async for evt in self._stream_turn_body(ctx):
                yield evt
        finally:
            _conn_mgr = ctx.extra.pop("_connector_session_manager", None)
            if _conn_mgr is not None:
                try:
                    await _conn_mgr.close_all()
                except Exception:  # noqa: BLE001 — shutdown must never raise
                    logger.warning(
                        "connector session close failed for run %s", ctx.run_id,
                        exc_info=True,
                    )

    async def _stream_turn_body(
        self, ctx: AgentTurnContext
    ) -> AsyncIterator[AgentEvent]:
        db = ctx.db
        cfg = ctx.model_config
        assistant_msg = ctx.assistant_msg
        conversation_id = ctx.conversation.id

        # Server-side session memory (Hermes): per-conversation isolation +
        # per-user long-term scope. Other providers ignore these kwargs. The
        # try/except keeps injected test doubles with the old single-arg
        # signature working.
        try:
            provider = get_provider_for_config(
                cfg,
                session_id=str(conversation_id),
                session_key=f"user:{ctx.user.id}",
            )
        except TypeError:
            provider = get_provider_for_config(cfg)
        registry = get_default_registry()
        # Merge statically-configured MCP server tools into this run's registry
        # so the model is offered them and calls route through ToolGateway
        # (approval/audit/truncation/budget) like builtins. No-op when MCP is
        # unconfigured or disconnected (boot guard holds).
        from app.agents.mcp_client import merge_mcp_tools

        merge_mcp_tools(registry)
        # Per-tenant connector→session lifecycle (Task 9 follow-up): open a live
        # McpSession for each of THIS user's ENABLED connectors and merge their
        # tools into the same registry through the same gateway path (no parallel
        # execution path). Tenant-scoped (only this user's connectors load),
        # graceful (a broken connector is isolated + skipped, never crashes the
        # run), credentials decrypted in-memory only. Sessions are closed by the
        # stream_turn wrapper's finally (graceful shutdown).
        user_id = ctx.user.id if ctx.user is not None else None
        if user_id is not None:
            _conn_mgr = None
            try:
                from app.connectors.sessions import ConnectorSessionManager

                _conn_mgr = ConnectorSessionManager(db)
                _conn_registry = await _conn_mgr.open_for_user(user_id)
                merge_mcp_tools(registry, mcp_registry=_conn_registry)
                # Stash so stream_turn's finally tears it down at run end.
                ctx.extra["_connector_session_manager"] = _conn_mgr
            except Exception:  # noqa: BLE001 — connector tools must never crash the run
                logger.warning(
                    "connector→session merge failed for run %s; skipping",
                    ctx.run_id, exc_info=True,
                )
                # Defensive: if a manager was created but not stashed (failure
                # between open and stash), close any sessions it opened so they
                # don't leak. stream_turn's finally only sees stashed managers.
                if _conn_mgr is not None:
                    try:
                        await _conn_mgr.close_all()
                    except Exception:  # noqa: BLE001
                        pass
        settings = get_settings()
        guard = ctx.budget_guard
        if guard is None:
            guard = BudgetGuard(
                BudgetLimits.from_settings(
                    settings,
                    ctx.extra.get("budget_overrides"),
                    allow_increase=bool(
                        ctx.extra.get("budget_policy_authorized", False)
                    ),
                )
            )
            ctx.budget_guard = guard
        gateway = ToolGateway(
            db,
            conversation_id=conversation_id,
            assistant_message_id=assistant_msg.id,
            run_id=ctx.run_id,
            user=ctx.user,
            registry=registry,
            # Match the persisted-output cap to the run's tool-output budget so a
            # limit above the gateway default isn't silently capped lower here.
            max_result_chars=guard.limits.max_tool_output_chars,
        )

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

        # Capability-driven provider parameters (B6): the ModelConfig switches
        # now map to real request parameters.
        #   - supports_parallel_tools → OpenAI parallel_tool_calls
        #   - supports_reasoning_effort → reasoning_effort (per-request
        #     override via request extra; default "medium")
        #   - supports_structured_output → response_format json_object when a
        #     caller opts in via ctx.extra["structured_output"]
        _extra: dict[str, Any] = {}
        if tool_schemas and getattr(cfg, "supports_parallel_tools", False):
            _extra["parallel_tool_calls"] = True
        if getattr(cfg, "supports_reasoning_effort", False):
            _extra["reasoning_effort"] = (
                ctx.extra.get("reasoning_effort") or "medium"
            )
        if getattr(cfg, "supports_structured_output", False) and ctx.extra.get(
            "structured_output"
        ):
            _extra["response_format"] = {"type": "json_object"}

        options = ChatOptions(
            temperature=cfg.temperature,
            top_p=cfg.top_p,
            max_tokens=safe_positive_int(cfg.max_tokens, 1024),
            output_token_parameter=provider_output_token_parameter(provider),
            tools=tool_schemas,
            tool_choice="auto",
            extra=_extra,
            retry_gate=lambda _attempt: _gate_model_retry(guard),
        )

        # Scope C: surface even single-agent native turns in the agent panel.
        # One 'assistant' node runs for the whole turn; plan/answer are phases.
        _graph = build_single_agent_graph(ctx.user_content)
        _graph.run_id = str(ctx.run_id)
        _assistant_started = _now_iso()
        _node_terminal = "completed"
        yield ev_agent_graph(run_id=ctx.run_id, graph=_graph.to_public_dict())
        yield ev_agent_status(
            run_id=ctx.run_id, agent_id="assistant", status="running",
            started_at=_assistant_started, task_title="理解问题并生成回答",
        )
        yield ev_run_status(
            run_id=ctx.run_id, status="running", current_agent_ids=["assistant"],
        )

        # Agent mode: classify intent + publish a short structured plan. The
        # plan is shown to the user (plan_created) instead of raw chain-of-thought.
        if ctx.enable_tools:
            yield ev_step_started(
                step_id="plan", title="理解问题与规划", step_type="llm", agent="assistant",
            )
            intent = classify_intent(ctx.user_content)
            ctx.extra["intent"] = intent
            plan_summary, plan_steps = build_plan(intent, ctx.user_content)
            yield ev_plan_created(summary=plan_summary, steps=plan_steps)
            yield ev_step_completed(step_id="plan")

        working: list[dict[str, Any]] = list(ctx.messages)
        finish_reason = "stop"
        usage_rounds: list[dict[str, Any]] = []
        model_dispatches = 0

        # Task 7: mid-run compaction wiring (gated, LLM-backed). The per-round
        # loop compacts the in-flight transcript when it crosses the soft budget
        # threshold, preserving older context as a summary instead of letting
        # the FIFO trim silently drop it. The summarize_fn reuses the SAME
        # provider-backed summarize_prefix the post-turn summarization uses
        # (NOT the heuristic chat_service stub — that stays a unit-test double).
        # ``_midrun_manager._summarize_fn`` is never invoked because the runtime
        # path goes through ``compact_async`` + ``summarize_fn_async``.
        _midrun_manager = ContextManager(
            summarize_fn=lambda older: "",
            # Production spill writer (Task 10): a mid-run spill persists as a
            # real tenant-scoped Artifact using the auth context bound by the
            # chat turn. Falls through to temp-file when no turn is bound.
            spill_writer=production_spill_writer,
        )

        async def _summarize_older_for_midrun(older_msgs: list[dict[str, Any]]) -> str:
            return await summarize_prefix(provider, older_msgs)

        def record_usage_once(
            usage: dict[str, Any] | None,
            *,
            usage_id: str,
            model_usage: bool,
        ) -> None:
            if usage is None:
                return
            usage_rounds.append(usage)
            cost = usage.get("cost_usd")
            if cost is None and model_usage:
                cost = usage_cost(cfg.model_name, usage)
            guard.add_usage(usage, cost_usd=cost, usage_id=usage_id)
            ctx.extra["usage"] = aggregate_usage(usage_rounds)
        policy = ContinuationPolicy(
            max_rounds=settings.AUTO_CONTINUATION_MAX_ROUNDS
        )
        continuation_round = 0
        is_continuation_round = False

        async def persist_continuation(checkpoint: dict[str, Any]) -> None:
            persist_checkpoint = ctx.extra.get("persist_continuation_checkpoint")
            if callable(persist_checkpoint):
                await persist_checkpoint(checkpoint)
                return
            from app.services.chat_service import _persist_continuation_checkpoint

            session_factory = ctx.extra.get("persistence_session_factory") or AsyncSessionLocal
            async with db_mutation_scope(ctx.extra.get("persistence_lock")):
                await _persist_continuation_checkpoint(
                    session_factory, assistant_msg, ctx.run_id, checkpoint
                )

        async def persist_cancelled_continuation() -> dict[str, Any]:
            checkpoint = {
                "round": continuation_round,
                "max_rounds": policy.max_rounds,
                "status": "cancelled",
            }
            ctx.extra["continuation"] = checkpoint
            assistant_msg.metadata_ = {
                **(assistant_msg.metadata_ or {}),
                "continuation": checkpoint,
            }
            await persist_continuation(checkpoint)
            return checkpoint

        yield ev_step_started(
            step_id="answer", title="生成回答", step_type="llm", agent="assistant",
        )
        try:
            while True:
                # Cooperative cancel: /api/agent-runs/{id}/cancel sets this event.
                ctl = ctx.extra.get("run_control")
                if ctl is not None and ctl.cancel.is_set():
                    finish_reason = "cancelled"
                    _node_terminal = "cancelled"
                    current_checkpoint = ctx.extra.get("continuation")
                    if (
                        continuation_round
                        and isinstance(current_checkpoint, dict)
                        and current_checkpoint.get("status") == "continuing"
                    ):
                        await persist_cancelled_continuation()
                    break
                try:
                    guard.enter_step()
                except BudgetExceeded as exc:
                    finish_reason = "budget"
                    logger.info("native run hit budget: %s", exc.reason)
                    break

                accumulated: list[str] = []
                novel_parts: list[str] = []
                pending_tool_calls: list[ToolCallDef] = []
                round_usage: dict[str, Any] | None = None
                continuation_buffer = (
                    ContinuationBuffer(
                        assistant_msg.content or "",
                        comparison_window=policy.comparison_window,
                    )
                    if is_continuation_round
                    else None
                )

                # Tool results expand ``working`` between rounds. Re-admit the
                # exact payload immediately before every provider dispatch so
                # a large result can never bypass the model context limit.
                from app.services.chat_service import (
                    _admit_and_trim_history,
                    _estimate_tokens,
                )
                tool_schema_tokens = (
                    _estimate_tokens(
                        json.dumps(options.tools, ensure_ascii=False, default=str),
                        cfg.model_name,
                    )
                    if options.tools
                    else 0
                )
                # Task 7: gated mid-run compaction. Between rounds, tool results
                # expand ``working``. If the in-flight transcript crossed the
                # soft compaction threshold, summarize the older prefix (keeping
                # the recent tail + tool pairs verbatim) BEFORE the FIFO trim —
                # so context the FIFO trim would silently drop is preserved as a
                # summary. Gated by should_compact_midrun: when the budget is
                # NOT exceeded mid-run, behavior is IDENTICAL to today (no
                # summary round-trip, no message changes). Best-effort: a
                # summarization failure falls through to the FIFO trim below.
                _caps = capabilities_from_config(cfg)
                _input_budget = calculate_prompt_budget(
                    _caps,
                    requested_output=_caps.max_output_tokens,
                    tool_schema_tokens=tool_schema_tokens,
                ).input_tokens
                if _midrun_manager.should_compact_midrun(
                    working, input_budget=_input_budget
                ):
                    try:
                        _pre_compact_n = len(working)
                        working = await _midrun_manager.compact_async(
                            working,
                            input_budget=_input_budget,
                            summarize_fn_async=_summarize_older_for_midrun,
                        )
                        ctx.extra["midrun_compaction"] = True
                        ctx.extra["midrun_compaction_from"] = _pre_compact_n
                        logger.info(
                            "mid-run compaction fired: %d -> %d msgs (budget=%d)",
                            _pre_compact_n, len(working), _input_budget,
                        )
                    except Exception:  # noqa: BLE001 — best-effort; never block
                        logger.warning(
                            "mid-run compaction failed; falling back to FIFO trim",
                            exc_info=True,
                        )
                # FIFO trim remains the authoritative admission backstop; after
                # compaction it is typically a no-op. It guarantees the final
                # transcript fits and fast-fails an impossible latest turn.
                try:
                    working = _admit_and_trim_history(
                        working,
                        cfg,
                        model_name=cfg.model_name,
                        tool_schema_tokens=tool_schema_tokens,
                    )
                except PromptAdmissionError as exc:
                    finish_reason = "budget"
                    ctx.extra["admission_error_code"] = exc.code
                    ctx.extra["finish_reason"] = finish_reason
                    ctx.extra["budget"] = guard.snapshot()
                    logger.info("native provider dispatch rejected: %s", exc.code)
                    yield ev_error(
                        code=exc.code,
                        message=str(exc),
                        usage=aggregate_usage(usage_rounds),
                    )
                    for _evt in _native_terminal_events(
                        ctx, "failed", _assistant_started
                    ):
                        yield _evt
                    return

                # Backpressure (bound concurrent in-flight model calls) + circuit
                # breaker (fast-fail when this provider endpoint is down).
                from app.core.concurrency import model_breaker, model_limiter
                _breaker_key = f"{getattr(provider, 'base_url', '')}|{getattr(provider, 'model', '')}"
                model_breaker().before(_breaker_key)
                limiter = model_limiter()
                limiter_acquired = False
                model_dispatches += 1
                dispatch_usage_id = f"native:model:{model_dispatches}"
                try:
                    guard.check()
                    async with asyncio.timeout(guard.remaining_seconds):
                        await limiter.acquire()
                        limiter_acquired = True
                        # supports_stream=False (B6): the model config declares
                        # this endpoint can't stream → one buffered chat call,
                        # surfaced to the UI as a single delta. The chat SSE
                        # contract toward the frontend is unchanged.
                        if not getattr(cfg, "supports_stream", True):
                            buffered = await provider.chat(working, options)
                            async def _one_delta():
                                yield buffered
                            _delta_iter = _one_delta()
                        else:
                            _delta_iter = provider.stream_chat(working, options)
                        async for delta in _delta_iter:
                            ctl = ctx.extra.get("run_control")
                            if ctl is not None and ctl.cancel.is_set():
                                finish_reason = "cancelled"
                                _node_terminal = "cancelled"
                                break
                            if ctl is not None:
                                # Drain appended user instructions into the
                                # working context (single-agent parity with the
                                # CrewAI path's between-stage injection).
                                for instr in ctl.drain_instructions():
                                    await append_event_safe(
                                        ctx.db, ctx.run_id,
                                        "run_instruction_received",
                                        {"instruction": instr},
                                    )
                                    working = working + [
                                        {
                                            "role": "user",
                                            "content": f"[追加指引] {instr}",
                                        }
                                    ]
                                if ctl.is_paused():
                                    await append_event_safe(
                                        ctx.db, ctx.run_id, "run_paused",
                                        {"reason": "user"},
                                    )
                                    yield ev_run_paused(run_id=ctx.run_id, reason="user")
                                    while ctl.is_paused() and not ctl.cancel.is_set():
                                        await asyncio.sleep(0.1)
                                    if not ctl.cancel.is_set():
                                        await append_event_safe(
                                            ctx.db, ctx.run_id, "run_resumed", {}
                                        )
                                        yield ev_run_resumed(run_id=ctx.run_id)
                                if ctl.cancel.is_set():
                                    finish_reason = "cancelled"
                                    _node_terminal = "cancelled"
                                    break
                            if delta.content:
                                accumulated.append(delta.content)
                                novel = (
                                    continuation_buffer.feed(delta.content)
                                    if continuation_buffer is not None
                                    else delta.content
                                )
                                if novel:
                                    novel_parts.append(novel)
                                    yield ev_token(delta=novel)
                            if delta.meta and "hermes_tool" in delta.meta:
                                # Hermes server-side tool progress — re-emit as
                                # standard tool_call/tool_result events so the
                                # existing progress UI renders them.
                                prog = delta.meta["hermes_tool"]
                                _hid = str(prog.get("toolCallId") or prog.get("tool") or "")
                                if prog.get("status") == "running":
                                    yield ev_tool_call(
                                        id=_hid,
                                        name=str(prog.get("tool") or "tool"),
                                        arguments={"label": prog.get("label", ""), "emoji": prog.get("emoji", "")},
                                        agent_id="assistant",
                                    )
                                else:
                                    yield ev_tool_result(
                                        id=_hid,
                                        name=str(prog.get("tool") or "tool"),
                                        ok=prog.get("status") != "error",
                                        agent_id="assistant",
                                    )
                            if delta.tool_calls:
                                pending_tool_calls.extend(delta.tool_calls)
                            if delta.finish_reason:
                                finish_reason = delta.finish_reason
                            if delta.usage:
                                # A provider may emit multiple snapshots in one
                                # call. Keep only its final cumulative snapshot.
                                round_usage = delta.usage
                    guard.check()
                    model_breaker().record_success(_breaker_key)
                except (BudgetExceeded, TimeoutError) as exc:
                    if continuation_buffer is not None:
                        buffered_novel = continuation_buffer.flush()
                        if buffered_novel:
                            novel_parts.append(buffered_novel)
                            yield ev_token(delta=buffered_novel)
                    assistant_msg.content = (assistant_msg.content or "") + "".join(
                        novel_parts
                    )
                    record_usage_once(
                        round_usage,
                        usage_id=dispatch_usage_id,
                        model_usage=True,
                    )
                    finish_reason = "budget"
                    _node_terminal = "failed"
                    reason = (
                        exc.reason
                        if isinstance(exc, BudgetExceeded)
                        else f"time budget ({guard.limits.max_runtime_seconds}s) exceeded"
                    )
                    ctx.extra["budget_exceeded_reason"] = reason
                    logger.info("native model dispatch hit budget: %s", reason)
                    break
                except PromptAdmissionError as exc:
                    if continuation_buffer is not None:
                        buffered_novel = continuation_buffer.flush()
                        if buffered_novel:
                            novel_parts.append(buffered_novel)
                            yield ev_token(delta=buffered_novel)
                    assistant_msg.content = (assistant_msg.content or "") + "".join(
                        novel_parts
                    )
                    record_usage_once(
                        round_usage,
                        usage_id=dispatch_usage_id,
                        model_usage=True,
                    )
                    finish_reason = "budget"
                    ctx.extra["admission_error_code"] = exc.code
                    ctx.extra["finish_reason"] = finish_reason
                    ctx.extra["budget"] = guard.snapshot()
                    logger.info("native provider payload rejected: %s", exc.code)
                    yield ev_error(
                        code=exc.code,
                        message=str(exc),
                        usage=ctx.extra.get("usage"),
                    )
                    for _evt in _native_terminal_events(
                        ctx, "failed", _assistant_started
                    ):
                        yield _evt
                    return
                except asyncio.CancelledError:
                    if continuation_buffer is not None:
                        buffered_novel = continuation_buffer.flush()
                        if buffered_novel:
                            novel_parts.append(buffered_novel)
                            yield ev_token(delta=buffered_novel)
                    assistant_msg.content = (assistant_msg.content or "") + "".join(
                        novel_parts
                    )
                    record_usage_once(
                        round_usage,
                        usage_id=dispatch_usage_id,
                        model_usage=True,
                    )
                    if continuation_round:
                        checkpoint = {
                            "round": continuation_round,
                            "max_rounds": policy.max_rounds,
                            "status": "cancelled",
                        }
                        ctx.extra["continuation"] = checkpoint
                        assistant_msg.metadata_ = {
                            **(assistant_msg.metadata_ or {}),
                            "continuation": checkpoint,
                        }
                        await persist_continuation(checkpoint)
                    raise
                except ProviderError as exc:
                    model_breaker().record_failure(_breaker_key)
                    # Fold this round's partial text before surfacing the error,
                    # so a mid-stream timeout/network failure still preserves
                    # everything generated so far.
                    if continuation_buffer is not None:
                        buffered_novel = continuation_buffer.flush()
                        if buffered_novel:
                            novel_parts.append(buffered_novel)
                            yield ev_token(delta=buffered_novel)
                    assistant_msg.content = (assistant_msg.content or "") + "".join(
                        novel_parts
                    )
                    record_usage_once(
                        round_usage,
                        usage_id=dispatch_usage_id,
                        model_usage=True,
                    )
                    logger.exception("provider error in native run: %s", exc)
                    yield ev_error(
                        code=getattr(exc, "code", "provider_error"),
                        message=str(exc),
                        usage=ctx.extra.get("usage"),
                    )
                    _node_terminal = "failed"
                    for _evt in _native_terminal_events(ctx, "failed", _assistant_started):
                        yield _evt
                    return
                except Exception as exc:  # noqa: BLE001
                    model_breaker().record_failure(_breaker_key)
                    if continuation_buffer is not None:
                        buffered_novel = continuation_buffer.flush()
                        if buffered_novel:
                            novel_parts.append(buffered_novel)
                            yield ev_token(delta=buffered_novel)
                    assistant_msg.content = (assistant_msg.content or "") + "".join(
                        novel_parts
                    )
                    record_usage_once(
                        round_usage,
                        usage_id=dispatch_usage_id,
                        model_usage=True,
                    )
                    logger.exception("unexpected provider stream error: %s", exc)
                    yield ev_error(
                        code="internal",
                        message="Internal error during generation",
                        usage=ctx.extra.get("usage"),
                    )
                    _node_terminal = "failed"
                    for _evt in _native_terminal_events(
                        ctx, "failed", _assistant_started
                    ):
                        yield _evt
                    return
                finally:
                    if limiter_acquired:
                        limiter.release()

                if continuation_buffer is not None:
                    buffered_novel = continuation_buffer.flush()
                    if buffered_novel:
                        novel_parts.append(buffered_novel)
                        yield ev_token(delta=buffered_novel)
                record_usage_once(
                    round_usage,
                    usage_id=dispatch_usage_id,
                    model_usage=True,
                )
                # Fold this round's streamed text into the assistant message so a
                # mid-loop disconnect still leaves recoverable content.
                assistant_msg.content = (assistant_msg.content or "") + "".join(
                    novel_parts
                )
                try:
                    guard.check()
                except BudgetExceeded as exc:
                    finish_reason = "budget"
                    _node_terminal = "failed"
                    ctx.extra["budget_exceeded_reason"] = exc.reason
                    logger.info("native model usage hit budget: %s", exc.reason)
                    break

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
                            id=tc.id, name=tc.name, arguments=args, dangerous=dangerous,
                            agent_id="assistant",
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
                            execution = await _execute_tool_with_budget(
                                guard,
                                gateway,
                                tool_call_id=tc.id,
                                tool_name=tc.name,
                                arguments=args,
                            )
                        except BudgetExceeded as exc:
                            finish_reason = "budget"
                            _node_terminal = "failed"
                            ctx.extra["budget_exceeded_reason"] = exc.reason
                            logger.info("native run hit tool budget: %s", exc.reason)
                            # Surface an error tool_result so the transcript is coherent.
                            yield ev_tool_result(
                                id=tc.id, name=tc.name, ok=False,
                                result=None, error=f"budget exceeded: {exc.reason}",
                                agent_id="assistant",
                            )
                            break

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
                                guard.check()
                                try:
                                    async with asyncio.timeout(guard.remaining_seconds):
                                        wr = await approval_coordinator.wait(
                                            execution.approval_id
                                        )
                                except TimeoutError:
                                    guard.check()
                                    wr.decision = "timed_out"
                            except BudgetExceeded as exc:
                                finish_reason = "budget"
                                _node_terminal = "failed"
                                ctx.extra["budget_exceeded_reason"] = exc.reason
                            finally:
                                approval_coordinator.release(execution.approval_id)

                            if finish_reason == "budget":
                                yield ev_tool_result(
                                    id=tc.id,
                                    name=tc.name,
                                    ok=False,
                                    error=(
                                        "budget exceeded: "
                                        + ctx.extra["budget_exceeded_reason"]
                                    ),
                                    agent_id="assistant",
                                )
                                break

                            if wr.decision == "cancelled":
                                await self._set_run_status(ctx, "cancelled")
                                raise asyncio.CancelledError()
                            if wr.decision == "approved":
                                await self._set_run_status(ctx, "running")
                                # Re-run through the gateway; it now finds the approved
                                # ToolApproval and executes for real.
                                try:
                                    execution = await _execute_tool_with_budget(
                                        guard,
                                        gateway,
                                        tool_call_id=tc.id,
                                        tool_name=tc.name,
                                        arguments=args,
                                    )
                                except BudgetExceeded as exc:
                                    finish_reason = "budget"
                                    _node_terminal = "failed"
                                    ctx.extra["budget_exceeded_reason"] = exc.reason
                                    yield ev_tool_result(
                                        id=tc.id,
                                        name=tc.name,
                                        ok=False,
                                        error=f"budget exceeded: {exc.reason}",
                                        agent_id="assistant",
                                    )
                                    break
                            elif wr.decision == "rejected":
                                await self._set_run_status(ctx, "running")
                                reason = wr.reason or "rejected by user"
                                yield ev_tool_result(
                                    id=tc.id, name=tc.name, ok=False,
                                    result=None, error=reason,
                                    agent_id="assistant",
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
                                    agent_id="assistant",
                                )
                                working.append({
                                    "role": "tool",
                                    "tool_call_id": tc.id,
                                    "name": tc.name,
                                    "content": json.dumps({"error": "approval timed out"}, ensure_ascii=False),
                                })
                                continue

                        tool_usage = getattr(execution, "usage", None)
                        if tool_usage:
                            record_usage_once(
                                tool_usage,
                                usage_id=f"native:tool:{tc.id}",
                                model_usage=False,
                            )
                        yield ev_tool_result(
                            id=tc.id,
                            name=tc.name,
                            ok=execution.ok,
                            # SSE/UI gets the UNTRUNCATED result so the frontend can
                            # parse the full web_search JSON into 「来源」(the model
                            # context still gets the truncated content via
                            # to_openai_tool_message). Falls back to the (truncated)
                            # content when full_result isn't present.
                            result=(
                                execution.full_result
                                if execution.ok and execution.full_result is not None
                                else (
                                    execution.result.get("content")
                                    if execution.ok and isinstance(execution.result, dict)
                                    else execution.result
                                )
                            ),
                            error=execution.error,
                            agent_id="assistant",
                            usage=tool_usage,
                        )
                        working.append(
                            _bounded_tool_message(
                                execution, guard.limits.max_tool_output_chars
                            )
                        )
                        try:
                            guard.check()
                        except BudgetExceeded as exc:
                            finish_reason = "budget"
                            _node_terminal = "failed"
                            ctx.extra["budget_exceeded_reason"] = exc.reason
                            break

                    # Re-stream with the tool results folded in.
                    if finish_reason == "budget":
                        break
                    finish_reason = "stop"  # reset; the next round decides
                    is_continuation_round = False
                    continue

                ctl = ctx.extra.get("run_control")
                if (
                    finish_reason == "length"
                    and ctl is not None
                    and ctl.cancel.is_set()
                ):
                    finish_reason = "cancelled"
                    _node_terminal = "cancelled"
                    checkpoint = {
                        "round": continuation_round,
                        "max_rounds": policy.max_rounds,
                        "status": "cancelled",
                    }
                    ctx.extra["continuation"] = checkpoint
                    assistant_msg.metadata_ = {
                        **(assistant_msg.metadata_ or {}),
                        "continuation": checkpoint,
                    }
                if policy.should_continue(
                    finish_reason,
                    continuation_round,
                    pending_tool_calls=bool(pending_tool_calls),
                    cancelled=finish_reason == "cancelled",
                ):
                    working.append(
                        {"role": "assistant", "content": "".join(accumulated)}
                    )
                    working.append(
                        {"role": "user", "content": _CONTINUE_INSTRUCTION}
                    )
                    continuation_round += 1
                    checkpoint = {
                        "round": continuation_round,
                        "max_rounds": policy.max_rounds,
                        "status": "continuing",
                    }
                    ctx.extra["continuation"] = checkpoint
                    assistant_msg.metadata_ = {
                        **(assistant_msg.metadata_ or {}),
                        "continuation": checkpoint,
                    }
                    try:
                        await persist_continuation(checkpoint)
                    except asyncio.CancelledError:
                        await persist_cancelled_continuation()
                        raise
                    ctl = ctx.extra.get("run_control")
                    if ctl is not None and ctl.cancel.is_set():
                        finish_reason = "cancelled"
                        _node_terminal = "cancelled"
                        await persist_cancelled_continuation()
                        break
                    is_continuation_round = True
                    continue
                if finish_reason == "length" and not pending_tool_calls:
                    checkpoint = {
                        "round": continuation_round,
                        "max_rounds": policy.max_rounds,
                        "status": "maxed",
                    }
                    ctx.extra["continuation"] = checkpoint
                    assistant_msg.metadata_ = {
                        **(assistant_msg.metadata_ or {}),
                        "continuation": checkpoint,
                    }
                elif continuation_round:
                    checkpoint = {
                        "round": continuation_round,
                        "max_rounds": policy.max_rounds,
                        "status": (
                            "cancelled" if finish_reason == "cancelled" else "completed"
                        ),
                    }
                    ctx.extra["continuation"] = checkpoint
                    assistant_msg.metadata_ = {
                        **(assistant_msg.metadata_ or {}),
                        "continuation": checkpoint,
                    }

                terminal_checkpoint = ctx.extra.get("continuation")
                if (
                    isinstance(terminal_checkpoint, dict)
                    and terminal_checkpoint.get("status")
                    in {"maxed", "completed", "cancelled"}
                ):
                    await persist_continuation(terminal_checkpoint)

                # No further tool calls — done.
                break
        except ProviderError as exc:
            yield ev_error(
                code="provider_error",
                message=str(exc),
                usage=aggregate_usage(usage_rounds),
            )
            for _evt in _native_terminal_events(ctx, "failed", _assistant_started):
                yield _evt
            return
        except Exception as exc:  # noqa: BLE001 — never let the stream die silently
            logger.exception("unexpected error in native run: %s", exc)
            yield ev_error(
                code="internal",
                message="Internal error during generation",
                usage=aggregate_usage(usage_rounds),
            )
            for _evt in _native_terminal_events(ctx, "failed", _assistant_started):
                yield _evt
            return

        if continuation_round and finish_reason == "cancelled":
            checkpoint = {
                "round": continuation_round,
                "max_rounds": policy.max_rounds,
                "status": "cancelled",
            }
            ctx.extra["continuation"] = checkpoint
            assistant_msg.metadata_ = {
                **(assistant_msg.metadata_ or {}),
                "continuation": checkpoint,
            }
        ctx.extra["finish_reason"] = finish_reason
        budget_snapshot = guard.snapshot()
        ctx.extra["budget"] = budget_snapshot
        _meta = {
            **(assistant_msg.metadata_ or {}),
            "budget": budget_snapshot,
        }
        # Persist any spilled artifact handles from this run's tool calls so
        # the frontend renders downloadable chips (Task-10 artifact wiring).
        # getattr guard: test fakes may subclass/replace the gateway without
        # the spill-tracking API.
        _drain = getattr(gateway, "drain_spilled_handles", None)
        _handles = _drain() if callable(_drain) else []
        if _handles:
            _meta["artifacts"] = _handles
            ctx.extra["spilled_artifacts"] = _handles
        assistant_msg.metadata_ = _meta
        if finish_reason == "budget":
            reason = (
                ctx.extra.get("budget_exceeded_reason")
                or budget_snapshot.get("reason")
                or "agent execution budget exhausted"
            )
            yield ev_error(
                code="agent_budget_exceeded",
                message=f"Agent execution budget exceeded: {reason}",
                usage=aggregate_usage(usage_rounds),
                finish_reason="budget",
                budget=budget_snapshot,
            )
        for _evt in _native_terminal_events(ctx, _node_terminal, _assistant_started):
            yield _evt
        yield ev_done(
            message_id=assistant_msg.id,
            finish_reason=finish_reason,
            usage=aggregate_usage(usage_rounds),
            budget=budget_snapshot,
        )

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
async def _execute_tool_with_budget(
    guard: BudgetGuard,
    gateway: ToolGateway,
    **kwargs: Any,
):
    """Gate and time-bound one tool attempt against the run deadline."""
    guard.check()
    try:
        async with asyncio.timeout(guard.remaining_seconds):
            execution = await gateway.execute(**kwargs)
    except TimeoutError as exc:
        raise BudgetExceeded(
            f"time budget ({guard.limits.max_runtime_seconds}s) exceeded"
        ) from exc
    guard.check()
    return execution


def _bounded_tool_message(execution: Any, max_chars: int) -> dict[str, Any]:
    """Apply the per-run observation cap before a tool result reaches a model."""
    if isinstance(execution, ToolExecution):
        return execution.to_openai_tool_message(max_chars=max_chars)
    # Compatibility for protocol/plugin implementations that expose the
    # legacy no-argument method. Invoke exactly once: an internal TypeError
    # must propagate rather than being mistaken for a signature mismatch.
    message = execution.to_openai_tool_message()
    content = message.get("content", "")
    if isinstance(content, str):
        # Truncate raw text directly; bounded_json_observation would json.dumps
        # the string and wrap it in quotes, double-encoding it for the model.
        message["content"] = content[: max(0, max_chars)]
    else:
        message["content"] = bounded_json_observation(content, max_chars=max_chars)
    return message


def _gate_model_retry(guard: BudgetGuard) -> float:
    """Charge and authorize one provider transport retry for this run."""
    guard.enter_step()
    guard.check()
    return guard.remaining_seconds


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


def _now_iso() -> str:
    """UTC now as an ISO-8601 string (for started_at / finished_at fields)."""
    return datetime.now(timezone.utc).isoformat()


def _elapsed_ms(start_iso: str) -> int:
    """Whole-ms between ``start_iso`` and now; 0 if unparseable."""
    try:
        start = datetime.fromisoformat(start_iso)
        return max(0, int((datetime.now(timezone.utc) - start).total_seconds() * 1000))
    except (TypeError, ValueError):
        return 0


def _native_terminal_events(ctx: AgentTurnContext, status: str, started_iso: str):
    """Yield the answer-step completion + node/run terminal status events.

    Single source for every exit point of :meth:`NativeChatRuntime.stream_turn`
    (normal done, cancel, budget, mid-stream/outer/generic errors). Iterated
    with ``for _evt in ...: yield _evt`` because ``yield from`` is illegal in
    an async generator.
    """
    yield ev_step_completed(
        step_id="answer", status="done" if status == "completed" else "error",
    )
    yield ev_agent_status(
        run_id=ctx.run_id, agent_id="assistant", status=status,
        finished_at=_now_iso(), duration_ms=_elapsed_ms(started_iso),
    )
    yield ev_run_status(run_id=ctx.run_id, status=status, current_agent_ids=[])
