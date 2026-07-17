"""ChatOrchestrator: runtime selection + run lifecycle.

Sits between :class:`~app.services.chat_service.ChatService` (which owns app
concerns: conversation/model/RAG/history/persistence) and a runtime (which owns
the model<->tool loop). For each turn the orchestrator:

  1. Persists an :class:`~app.models.AgentRun` row (status=running) and emits
     ``run_started``.
  2. Selects a runtime by ``execution_mode`` and capability (native always
     available; CrewAI when installed + enabled + requested).
  3. Forwards the runtime's events, intercepting the terminal ``done``/``error``
     to flip the run row to ``completed``/``failed``.
  4. On an unexpected exception, marks the run ``failed`` and emits ``error``.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.runtime.native_runtime import NativeChatRuntime
from app.agents.schemas import (
    AgentEvent,
    AgentTurnContext,
    ExecutionMode,
    RuntimeKind,
    ev_error,
    ev_run_started,
)
from app.core.config import get_settings
from app.models import AgentRun

logger = logging.getLogger(__name__)


class ChatOrchestrator:
    """Owns the AgentRun lifecycle and runtime dispatch."""

    def __init__(self) -> None:
        self._native = NativeChatRuntime()
        self._crewai: object | None = None
        self._crewai_checked = False

    # ------------------------------------------------------------------ #
    async def stream(self, ctx: AgentTurnContext) -> AsyncIterator[AgentEvent]:
        run = await self._create_run(ctx)
        yield ev_run_started(
            run_id=run.id,
            runtime=run.runtime,
            conversation_id=ctx.conversation.id,
            message_id=ctx.assistant_msg.id,
        )

        runtime = self._select_runtime(ctx)
        run.runtime = runtime.name
        run.status = "running"
        await ctx.db.commit()

        try:
            async for evt in runtime.stream_turn(ctx):
                if evt.kind in ("done", "error"):
                    await self._finalize_run(ctx.db, run, evt)
                yield evt
                if evt.kind in ("done", "error"):
                    return
        except Exception as exc:  # noqa: BLE001 — runtime should self-handle, but be safe
            logger.exception("runtime %s crashed: %s", runtime.name, exc)
            await self._fail_run(ctx.db, run, str(exc))
            yield ev_error(code="internal", message=str(exc))
            return

    # ------------------------------------------------------------------ #
    def _select_runtime(self, ctx: AgentTurnContext):
        """Pick native or CrewAI by execution_mode + availability.

        Phase 0: always native (CrewAI wired in Phase 1). The CrewAI branch is
        here so Phase 1 only needs to implement :meth:`_crewai_runtime`.
        """
        mode = ctx.execution_mode
        if mode == ExecutionMode.agent and self._crewai_available():
            cr = self._crewai_runtime()
            if cr is not None:
                return cr
        # chat + auto (+ agent fallback) all use native for now.
        return self._native

    def _crewai_available(self) -> bool:
        if not self._crewai_checked:
            self._crewai_checked = True
            settings = get_settings()
            if not getattr(settings, "CREWAI_ENABLED", False):
                self._crewai = None
            else:
                self._crewai = self._crewai_runtime()
        return self._crewai is not None

    def _crewai_runtime(self):  # pragma: no cover - implemented in Phase 1
        """Lazily build the CrewAI runtime. Returns None if crewai isn't importable."""
        try:
            from app.agents.runtime.crewai_runtime import CrewAIRuntime  # type: ignore
        except Exception as exc:  # noqa: BLE001
            logger.warning("CrewAI runtime unavailable, falling back to native: %s", exc)
            return None
        return CrewAIRuntime()

    # ------------------------------------------------------------------ #
    async def _create_run(self, ctx: AgentTurnContext) -> AgentRun:
        cfg = ctx.model_config
        snapshot = {
            "provider": cfg.provider,
            "model_name": cfg.model_name,
            "api_base_url": cfg.api_base_url,
            "temperature": cfg.temperature,
            "top_p": cfg.top_p,
            "max_tokens": cfg.max_tokens,
            "supports_tools": getattr(cfg, "supports_tools", False),
        }
        run = AgentRun(
            conversation_id=ctx.conversation.id,
            message_id=ctx.assistant_msg.id,
            user_id=ctx.user.id if ctx.user else None,
            runtime=RuntimeKind.native.value,
            flow_name="native_chat",
            status="running",
            current_step="",
            input={
                "content": ctx.user_content,
                "enable_tools": ctx.enable_tools,
                "execution_mode": ctx.execution_mode.value,
                "agent_profile": ctx.agent_profile,
                "knowledge_base_id": str(ctx.knowledge_base_id) if ctx.knowledge_base_id else None,
            },
            model_config_snapshot=snapshot,
            started_at=datetime.now(timezone.utc),
        )
        ctx.db.add(run)
        await ctx.db.flush()
        ctx.run_id = run.id
        ctx.extra["run_id"] = run.id
        return run

    async def _finalize_run(self, db: AsyncSession, run: AgentRun, evt: AgentEvent) -> None:
        run.finished_at = datetime.now(timezone.utc)
        run.output = dict(evt.data)
        if evt.kind == "done":
            run.status = "completed"
        else:
            run.status = "failed"
            run.error_message = str(evt.data.get("message", ""))
        try:
            await db.commit()
        except Exception:  # pragma: no cover - best effort
            logger.exception("failed to finalize agent_run %s", run.id)
            await db.rollback()

    async def _fail_run(self, db: AsyncSession, run: AgentRun, message: str) -> None:
        run.finished_at = datetime.now(timezone.utc)
        run.status = "failed"
        run.error_message = message
        try:
            await db.commit()
        except Exception:  # pragma: no cover
            await db.rollback()


# Module-level singleton — stateless aside from the lazy crewai cache.
chat_orchestrator = ChatOrchestrator()
