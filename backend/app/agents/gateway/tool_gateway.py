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

from app.agents.policies import (
    UnsafeSQLError,
    arguments_hash,
    expiry_from_now,
    is_expired,
    is_tool_allowed,
    preview,
    risk_level_for,
    risk_summary,
    should_require_approval,
    validate_readonly_sql,
)
from app.agents.schemas import RiskLevel, ToolExecution
from app.models import AgentStep, ToolApproval, ToolCall
from app.models.user import User
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
    ) -> None:
        self.db = db
        self.conversation_id = conversation_id
        self.assistant_message_id = assistant_message_id
        self.run_id = run_id
        self.user = user
        self._registry = registry or get_default_registry()
        self._step_seq = 0

    # ------------------------------------------------------------------ #
    async def execute(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        strict: bool | None = None,
    ) -> ToolExecution:
        """Run one tool call end to end. Never raises — failures become error results."""
        started = time.monotonic()
        args = arguments or {}

        # 1. Resolve.
        try:
            tool = self._registry.get(tool_name)
        except ToolError as exc:
            return await self._finalize(
                tool_call_id, tool_name, args, started,
                ok=False, status="error", error=str(exc),
            )

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

        # 4. Approval gate for dangerous tools.
        if should_require_approval(tool):
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
        except Exception as exc:  # noqa: BLE001 — isolate per-tool failures
            logger.exception("Tool %s raised unexpectedly", tool_name)
            return await self._finalize(
                tool_call_id, tool_name, args, started,
                ok=False, status="error", error=f"{type(exc).__name__}: {exc}",
            )

        # 6 + 7. Truncate + finalize (persists rows).
        content, truncated = _stringify_and_truncate(result)
        return await self._finalize(
            tool_call_id, tool_name, args, started,
            ok=True, status="success",
            result={"content": content, "truncated": truncated},
            truncated=truncated,
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
        step_type: str = "tool",
        step_status: str | None = None,
    ) -> ToolExecution:
        latency_ms = int((time.monotonic() - started) * 1000)

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
                agent_name="native",
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
            latency_ms=latency_ms,
        )


def _stringify_and_truncate(result: Any) -> tuple[str, bool]:
    """Return (text, truncated) for a tool result, capped at _MAX_RESULT_CHARS."""
    if isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(result)
    return text[:_MAX_RESULT_CHARS], len(text) > _MAX_RESULT_CHARS
