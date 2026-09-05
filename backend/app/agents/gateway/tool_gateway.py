"""Single source of truth for tool execution.

Both runtimes (native + CrewAI adapter) and the ``/api/tools/test`` path go
through :class:`ToolGateway`. It enforces, in order:

  1. **Resolution** — the tool must be registered.
  2. **Permission** — ``python_exec`` is disabled outside dev/sandbox.
  3. **SQL hardening** — ``db_query`` SQL must pass ``validate_readonly_sql``.
  4. **Approval** — dangerous tools need a valid (approved, non-expired)
     :class:`~app.models.ToolApproval` matching the exact arguments; otherwise a
     pending approval is created and a ``needs_approval`` result is returned.
  5. **Execution** — ``tool.run`` runs under a hard timeout backstop.
  6. **Audit** — a ``ToolCall`` row + an ``AgentStep`` row are persisted.
  7. **Truncation** — oversized output is capped before reaching the model.

The returned :class:`~app.agents.schemas.ToolExecution` carries the *real*
``ok`` (fixing the old always-``ok=True`` bug) so the SSE ``tool_result``
event reflects actual outcomes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.continuation import normalize_usage
from app.agents.policies import (
    UnsafeSQLError,
    arguments_hash,
    expiry_from_now,
    is_expired,
    is_tool_allowed,
    preview,
    risk_level_for,
    should_require_approval,
    validate_readonly_sql,
)
from app.agents.schemas import (
    ToolExecution,
    _bounded_text,
    bounded_json_preview,
)
from app.models import AgentStep, ToolApproval, ToolCall
from app.models.user import User
from app.observability import observe_counter, observe_histogram, observe_span
from app.quotas import QuotaExceeded, get_quota_service
from app.tools.base import BaseTool, ToolError, ToolRegistry
from app.tools.registry_init import get_default_registry

logger = logging.getLogger(__name__)

# Backstop timeout around tool.run (tools have their own internal timeouts).
TOOL_TIMEOUT_SECONDS = 30
# Cap persisted/displayed tool output so a runaway tool can't blow up the row
# or the next model prompt.
_MAX_RESULT_CHARS = 8000


class ToolGateway:
    """Stateful-per-run gateway. Construct one per agent run."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        conversation_id: uuid.UUID,
        assistant_message_id: uuid.UUID | None,
        run_id: uuid.UUID | None,
        user: User | None = None,
        registry: ToolRegistry | None = None,
        guardian_service: object | None = None,
        guardian_provider: object | None = None,
        guardian_breaker: object | None = None,
        pre_tool_use_handlers: list | None = None,
        max_result_chars: int = _MAX_RESULT_CHARS,
    ) -> None:
        self.db = db
        self.conversation_id = conversation_id
        self.assistant_message_id = assistant_message_id
        self.run_id = run_id
        self.user = user
        self._registry = registry or get_default_registry()
        self._step_seq = 0
        # Opaque artifact handles produced by oversized tool-result spills this
        # run (Task-10 wiring). The chat layer drains these after the turn and
        # renders them as downloadable chips in the assistant message.
        self._spilled_handles: list[str] = []
        # Current agent attribution (set per-stage by the CrewAI runtime via
        # :meth:`set_attribution`; native runtime leaves it blank). Persisted on
        # every AgentStep row so tools map back to the graph node that ran them.
        self._agent_id = ""
        self._task_id = ""
        # Optional Guardian pre-approval judge (Codex pattern). Off by default —
        # runtimes opt in by passing a GuardianService + provider (+ breaker). When
        # set, dangerous tools are LLM-judged before the human-approval gate: allow
        # auto-proceeds, deny/uncertain escalates to the human gate.
        self._guardian = guardian_service
        self._guardian_provider = guardian_provider
        self._guardian_breaker = guardian_breaker
        # Optional PreToolUse hooks (Codex pattern). Off unless the runtime passes
        # handlers. A handler can BLOCK (deny), REWRITE args (updated_input), or
        # note additional context (attached to the result for the runtime).
        self._pre_tool_use_handlers = list(pre_tool_use_handlers or [])
        if isinstance(max_result_chars, bool) or not isinstance(max_result_chars, int) or max_result_chars <= 0:
            raise ValueError("max_result_chars must be a positive integer")
        self._max_result_chars = max_result_chars

    def set_attribution(self, *, agent_id: str = "", task_id: str = "") -> None:
        """Set the agent/task id for subsequent tool executions in this run."""
        self._agent_id = agent_id or ""
        self._task_id = task_id or ""

    def drain_spilled_handles(self) -> list[str]:
        """Return and clear the artifact handles spilled during this run."""
        handles = list(self._spilled_handles)
        self._spilled_handles.clear()
        return handles

    # ------------------------------------------------------------------ #
    async def execute(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        strict: bool | None = None,
        agent_id: str | None = None,
        task_id: str | None = None,
    ) -> ToolExecution:
        """Run one tool call end to end. Never raises — failures become error results.

        ``agent_id``/``task_id`` (or the run-wide attribution set via
        :meth:`set_attribution`) are persisted on the AgentStep row so the tool
        maps back to the graph node that invoked it.
        """
        started = time.monotonic()
        args = arguments or {}
        # Per-call attribution overrides the run-wide default.
        aid = agent_id if agent_id is not None else self._agent_id
        tid = task_id if task_id is not None else self._task_id
        self._agent_id = aid
        self._task_id = tid

        # 0. Quota gate (Task 11b): a tenant over their per-run distinct-tool cap
        # is refused with an admin-visible reason. Gated on QUOTAS_ENABLED (off
        # in test + off by default) so existing tests are unaffected.
        quota_svc = get_quota_service()
        if quota_svc.enabled and self.user is not None:
            tenant = str(getattr(self.user, "id", self.user))
            try:
                await quota_svc.check_tool(
                    tenant,
                    tool_name,
                    run_id=str(self.run_id) if self.run_id is not None else None,
                )
            except QuotaExceeded as exc:
                return await self._finalize(
                    tool_call_id, tool_name, args, started,
                    ok=False, status="quota_exceeded", error=exc.reason,
                )

        # Observability (Task 11b): one span per tool execution; the outcome
        # attribute is set from the final status. Inert when exporters are off.
        with observe_span("tool.execute", tool=tool_name, run_id=str(self.run_id or "")) as sp:
            result = await self._execute_inner(
                tool_call_id, tool_name, args, started,
                strict=strict, agent_id=aid, task_id=tid,
            )
            try:
                sp.set_attribute("outcome", result.status)
            except Exception:  # noqa: BLE001 — never let tracing break the call
                pass
            return result

    async def _execute_inner(
        self,
        tool_call_id: str,
        tool_name: str,
        args: dict[str, Any],
        started: float,
        *,
        strict: bool | None,
        agent_id: str | None,
        task_id: str | None,
    ) -> ToolExecution:
        """Resolve → permission → approval → execute → audit (the original body)."""
        # 1. Resolve.
        try:
            tool = self._registry.get(tool_name)
        except ToolError as exc:
            return await self._finalize(
                tool_call_id, tool_name, args, started,
                ok=False, status="error", error=str(exc),
            )

        # 1b. PreToolUse hooks (optional, Codex pattern). External handlers run
        # before permission/approval: a handler can BLOCK (deny / continue=false),
        # REWRITE the arguments (updated_input), or note additional_context. Off
        # unless the runtime wires handlers in. Failure is fail-open (continue).
        if self._pre_tool_use_handlers:
            try:
                from app.agents.hooks.engine import HookEngine

                folded = HookEngine().run_pre_tool_use(
                    tool_name, args, self._pre_tool_use_handlers,
                    session_id=str(self.run_id or ""), turn_id=self._task_id or "", cwd="",
                )
            except Exception:
                logger.warning("pre_tool_use hook execution failed; continuing", exc_info=True)
                folded = None
            if folded is not None:
                denied = (
                    getattr(folded, "permission_decision", None) == "deny"
                    or getattr(folded, "continue_", True) is False
                )
                if denied:
                    return await self._finalize(
                        tool_call_id, tool_name, args, started,
                        ok=False, status="blocked",
                        error=f"blocked by PreToolUse hook: {getattr(folded, 'system_message', '') or 'denied'}",
                    )
                updated = getattr(folded, "updated_input", None)
                if isinstance(updated, dict):
                    args = updated  # rewrite the args used for the rest of execute()
                # additional_context is surfaced via the result so the runtime can
                # inject it into the model context (the gateway can't, mid-call).

        # 2. Permission (env-based; e.g. python_exec disabled in prod).
        if not is_tool_allowed(tool_name, self.user, strict=strict):
            return await self._finalize(
                tool_call_id, tool_name, args, started,
                ok=False, status="blocked",
                error=f"tool {tool_name!r} is not permitted in this environment",
            )

        # 3. SQL hardening for db_query (defense in depth; the tool also checks).
        if tool_name == "db_query":
            try:
                validate_readonly_sql(str(args.get("sql", "")))
            except UnsafeSQLError as exc:
                return await self._finalize(
                    tool_call_id, tool_name, args, started,
                    ok=False, status="blocked", error=str(exc),
                )

        # 3.5 Guardian pre-check (optional, Codex pattern). When a guardian judge
        # is wired in, dangerous tools are LLM-judged BEFORE the human-approval
        # gate: allow -> auto-proceed (skip the gate); deny/uncertain -> escalate
        # to the human gate below. The rejection circuit breaker aborts the turn
        # after repeated denials. Off unless the runtime passes a guardian.
        _auto_allowed = False
        if (
            self._guardian is not None
            and self._guardian_provider is not None
            and should_require_approval(tool)
        ):
            try:
                verdict = await self._guardian.judge(
                    action={"tool": tool_name, "arguments_preview": args},
                    provider=self._guardian_provider,
                )
            except Exception:
                logger.warning("guardian judge raised; treating as deny", exc_info=True)
                verdict = None
            if verdict is not None:
                if self._guardian_breaker is not None:
                    self._guardian_breaker.record(verdict)
                    if self._guardian_breaker.should_abort():
                        return await self._finalize(
                            tool_call_id, tool_name, args, started,
                            ok=False, status="blocked",
                            error="guardian: 连续多次拒绝，已中止本轮 (rejection circuit breaker)",
                        )
                _auto_allowed = verdict.allowed

        # 4. Approval gate for dangerous tools.
        if should_require_approval(tool) and not _auto_allowed:
            approval = await self._find_valid_approval(tool_name, args)
            if approval is None:
                if self.run_id is None:
                    # Legacy/test path has no run to attach an approval to: block.
                    return await self._finalize(
                        tool_call_id, tool_name, args, started,
                        ok=False, status="blocked",
                        error="此工具需要人工确认，但当前无运行上下文 (no run context)",
                    )
                ap = await self._create_pending_approval(tool, args)
                logger.info(
                    "tool %s requires approval (run=%s approval=%s)",
                    tool_name, self.run_id, ap.id,
                )
                return await self._finalize(
                    tool_call_id, tool_name, args, started,
                    ok=False, status="needs_approval",
                    error="此工具需要人工确认后才能执行 (approval required)",
                    approval_id=ap.id,
                    step_type="approval", step_status="waiting",
                )

        # 5. Execute under a timeout backstop.
        try:
            result = await asyncio.wait_for(tool.run(**args), timeout=TOOL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return await self._finalize(
                tool_call_id, tool_name, args, started,
                ok=False, status="timeout",
                error=f"tool {tool_name!r} timed out after {TOOL_TIMEOUT_SECONDS}s",
            )
        except ToolError as exc:
            return await self._finalize(
                tool_call_id, tool_name, args, started,
                ok=False, status="error", error=f"tool error: {exc}",
            )
        except Exception as exc:
            logger.exception("Tool %s raised unexpectedly", tool_name)
            return await self._finalize(
                tool_call_id, tool_name, args, started,
                ok=False, status="error", error=f"{type(exc).__name__}: {exc}",
            )

        # 6 + 7. Truncate + finalize (persists rows).
        usage = normalize_usage(result.get("usage")) if isinstance(result, dict) else None
        rendered_result = (
            {key: value for key, value in result.items() if key != "usage"}
            if isinstance(result, dict)
            else result
        )
        full_text, content, truncated = _stringify_and_truncate(
            rendered_result, self._max_result_chars
        )

        # 6b. Oversized tool output → real Artifact (Task-10 spill wiring).
        # When the rendered result was truncated, the model only sees the head;
        # spill the FULL text as a tenant-scoped Artifact and hand the model an
        # opaque ``artifact:<id>`` handle. The chat layer collects the handles
        # and renders download chips in the assistant message. Best-effort:
        # spill never blocks or fails the tool call.
        spilled_handle: str | None = None
        if truncated and full_text and self.user is not None:
            try:
                from app.agents.output_spill import spill_to_artifact

                _budget = self._max_result_chars // 4  # chars ≈ tokens×4
                _in_ctx, _handle = await spill_to_artifact(
                    self.db,
                    owner_id=self.user.id,
                    text=full_text,
                    budget_tokens=_budget,
                    media_type="application/json"
                    if isinstance(rendered_result, dict)
                    else "text/plain",
                    filename=f"{tool_name}-result.txt",
                    key=f"{tool_name}-{tool_call_id[:8]}",
                    source="spill",
                    run_id=self.run_id,
                    generator={"tool_name": tool_name, "tool_call_id": tool_call_id},
                )
                if _handle is not None:
                    spilled_handle = _handle.id
                    content = (
                        f"{content}\n\n[完整工具输出已保存为附件 {spilled_handle}，"
                        f"用户可下载查看全文]"
                    )
                    self._spilled_handles.append(spilled_handle)
            except Exception:
                logger.debug("tool-result spill failed for %s", tool_name, exc_info=True)

        return await self._finalize(
            tool_call_id, tool_name, args, started,
            ok=True, status="success",
            result={"content": content, "truncated": truncated},
            truncated=truncated,
            # The UNTRUNCATED stringified result, so the SSE tool_result event can
            # carry the full web_search/http_get payload to the UI — which needs the
            # complete JSON to extract 来源 (the 8000-char cap for the model context
            # used to cut the JSON mid-array and break source extraction).
            full_result=full_text,
            usage=usage,
        )

    # ------------------------------------------------------------------ #
    # Approval helpers
    # ------------------------------------------------------------------ #
    async def _find_valid_approval(self, tool_name: str, arguments: dict[str, Any]) -> ToolApproval | None:
        """Return an approved, non-expired approval for this exact (tool, args), or None."""
        ahash = arguments_hash(tool_name, arguments)
        result = await self.db.execute(
            select(ToolApproval).where(
                ToolApproval.run_id == self.run_id,
                ToolApproval.tool_name == tool_name,
                ToolApproval.arguments_hash == ahash,
                ToolApproval.status == "approved",
            )
        )
        for ap in result.scalars().all():
            if not is_expired(ap.expires_at):
                return ap
        return None

    async def _create_pending_approval(self, tool: BaseTool, arguments: dict[str, Any]) -> ToolApproval:
        level = risk_level_for(tool)
        ap = ToolApproval(
            run_id=self.run_id,
            conversation_id=self.conversation_id,
            user_id=self.user.id if self.user else None,
            tool_name=tool.name,
            arguments=arguments,
            arguments_hash=arguments_hash(tool.name, arguments),
            risk_level=level.value,
            status="pending",
            expires_at=expiry_from_now(),
        )
        self.db.add(ap)
        await self.db.flush()
        return ap

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    async def _finalize(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        started: float,
        *,
        ok: bool,
        status: str,
        result: Any = None,
        error: str | None = None,
        approval_id: uuid.UUID | None = None,
        truncated: bool = False,
        full_result: str | None = None,
        usage: dict[str, int | float] | None = None,
        step_type: str = "tool",
        step_status: str | None = None,
    ) -> ToolExecution:
        latency_ms = int((time.monotonic() - started) * 1000)

        # Observability (Task 11b): every finalize path records a tool.calls
        # counter (outcome = the status) + a tool.latency_ms histogram. Inert
        # when exporters are off; the test recorder captures both regardless.
        observe_counter("tool.calls", 1, tool=tool_name, outcome=status)
        observe_histogram("tool.latency_ms", latency_ms, tool=tool_name, outcome=status)

        # ToolCall row (always persisted — the historical audit trail).
        result_json: dict | None
        if ok:
            result_json = result if isinstance(result, dict) else {"content": result}
        elif approval_id is not None:
            result_json = {"needs_approval": True, "approval_id": str(approval_id)}
        else:
            result_json = None
        row = ToolCall(
            conversation_id=self.conversation_id,
            message_id=self.assistant_message_id,
            tool_name=tool_name,
            arguments=arguments,
            result=result_json,
            status="success" if ok else "error",
            error_message=error,
        )
        self.db.add(row)

        # AgentStep row (the structured execution trace). Skipped on the
        # legacy/test path where there is no run to attach the step to.
        if self.run_id is not None:
            self._step_seq += 1
            step = AgentStep(
                run_id=self.run_id,
                sequence=self._step_seq,
                step_type=step_type,
                agent_name=self._agent_id or "native",
                agent_id=self._agent_id,
                task_id=self._task_id,
                tool_name=tool_name,
                status=step_status or ("done" if ok else "error"),
                input_redacted=preview(arguments),
                output_redacted=preview(result_json) if isinstance(result_json, dict) else None,
                latency_ms=latency_ms,
            )
            self.db.add(step)

        try:
            await self.db.flush()
        except Exception:
            logger.exception("failed to flush tool audit rows for conversation %s", self.conversation_id)
            await self.db.rollback()

        # Audit the tool execution (best-effort, isolated session).
        from app.services import audit_service
        await audit_service.log(
            actor_id=self.user.id if self.user else None,
            action="tool_call",
            target=tool_name,
            detail={
                "run_id": str(self.run_id) if self.run_id else None,
                "conversation_id": str(self.conversation_id),
                "status": status,
                "ok": ok,
                "arguments": preview(arguments),
            },
        )

        return ToolExecution(
            ok=ok,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            status=status,
            result=result if ok else None,
            error=error,
            approval_id=approval_id,
            truncated=truncated,
            full_result=full_result,
            latency_ms=latency_ms,
            usage=usage,
            _max_output_chars=self._max_result_chars,
        )


def _stringify_and_truncate(
    result: Any, max_chars: int = _MAX_RESULT_CHARS
) -> tuple[str, str, bool]:
    """Return (full_text, truncated_text, truncated) for a tool result.

    ``truncated_text`` is capped at ``_MAX_RESULT_CHARS`` (for the model context +
    the persisted audit row); ``full_text`` is the UNTRUNCATED stringified result,
    surfaced via ``ToolExecution.full_result`` so the SSE event can carry the
    complete payload to the UI (which needs the full web_search JSON to extract
    「来源」 without a mid-array truncation breaking JSON.parse).
    """
    if isinstance(result, str):
        full = result
    else:
        try:
            full = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            full = str(result)
    if len(full) <= max_chars:
        return full, full, False
    bounded = (
        _bounded_text(full, max_chars=max_chars)
        if isinstance(result, str)
        else bounded_json_preview(full, max_chars=max_chars)
    )
    return full, bounded, True
