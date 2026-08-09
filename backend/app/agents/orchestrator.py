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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.runtime.native_runtime import NativeChatRuntime
from app.agents.run_controls import drop as drop_run_control, get_or_create as get_run_control
from app.agents.schemas import (
    AgentEvent,
    AgentTurnContext,
    ExecutionMode,
    RuntimeKind,
    ev_error,
    ev_intent_recognized,
    ev_run_started,
    ev_runtime_selected,
)
from app.core.config import get_settings
from app.models import AgentRun

logger = logging.getLogger(__name__)


@dataclass
class RuntimeSelection:
    """The explicit, observable result of runtime selection for one turn.

    Replaces the old silent ``native`` fallback: the orchestrator records WHAT
    was requested, WHAT actually ran, and WHY if it fell back. Emitted to the
    client as ``runtime_selected`` and persisted on the assistant message so the
    UI can never mistake a single-model fallback for a real multi-agent run.
    """

    requested_runtime: str
    selected_runtime: str
    available: bool
    fallback_reason: str | None
    multi_agent_requested: bool
    multi_agent_executed: bool
    agent_profile: str
    requested_mode: str
    effective_mode: str
    # True only when the answer came from the deterministic DemoStageExecutor
    # (canned, non-real). Drives the persistent UI warning; always False on the
    # public chat path because demo requires an explicit per-request opt-in.
    is_demo: bool = False


class ChatOrchestrator:
    """Owns the AgentRun lifecycle and runtime dispatch."""

    def __init__(self) -> None:
        self._native = NativeChatRuntime()
        self._crewai: object | None = None
        self._crewai_checked = False

    # ------------------------------------------------------------------ #
    async def stream(self, ctx: AgentTurnContext) -> AsyncIterator[AgentEvent]:
        run = await self._create_run(ctx)
        # Register cooperative pause/instruction controls for this run.
        ctx.extra["run_control"] = get_run_control(run.id)

        # Select the runtime BEFORE emitting run_started so the event reports the
        # real runtime (native vs crewai), not the placeholder "native".
        runtime, selection = self._select_runtime(ctx)
        ctx.extra["runtime_selection"] = selection
        run.runtime = runtime.name
        run.status = "running"
        await ctx.db.commit()

        yield ev_run_started(
            run_id=run.id,
            runtime=runtime.name,
            conversation_id=ctx.conversation.id,
            message_id=ctx.assistant_msg.id,
        )
        # Announce the explicit selection (requested vs effective, fallback
        # reason, multi_agent_executed). This is the anti-"fake-multi-agent"
        # signal: the frontend opens the agent panel only when
        # multi_agent_executed is true and shows a fallback warning otherwise.
        # ``is_demo`` additionally flags canned (non-real) answers so the UI can
        # warn the user the content is not a genuine model reply.
        yield ev_runtime_selected(
            run_id=run.id,
            requested_mode=selection.requested_mode,
            effective_mode=selection.effective_mode,
            requested_runtime=selection.requested_runtime,
            effective_runtime=selection.selected_runtime,
            agent_profile=selection.agent_profile,
            multi_agent_requested=selection.multi_agent_requested,
            multi_agent_executed=selection.multi_agent_executed,
            fallback_reason=selection.fallback_reason,
            is_demo=selection.is_demo,
        )
        # Surface the model-recognized intent so the client can show WHY a turn
        # went native vs research crew — the visible antidote to silent routing.
        _intent = ctx.extra.get("intent_decision")
        if _intent is not None:
            yield ev_intent_recognized(
                run_id=run.id,
                route=_intent.route,
                deliverable_kind=_intent.deliverable_kind,
                confidence=_intent.confidence,
                rationale=_intent.rationale,
                tool_hints=list(getattr(_intent, "tool_hints", []) or []),
                fragments=ctx.extra.get("intent_fragments") or [],
            )

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
        finally:
            drop_run_control(run.id)

    # ------------------------------------------------------------------ #
    def _select_runtime(self, ctx: AgentTurnContext) -> tuple[object, RuntimeSelection]:
        """Pick native or CrewAI and record an explicit :class:`RuntimeSelection`.

        A multi-agent request (debate / deep_research, i.e. the route set
        ``use_multi_agent``) is honored with the CrewAI runtime when available;
        otherwise it falls back to native with a VISIBLE ``fallback_reason`` and
        ``multi_agent_executed=False`` — never a silent single-model run that
        role-plays multiple agents.

        Demo isolation: the deterministic DemoStageExecutor (canned answers)
        runs ONLY on an explicit per-request opt-in (``request.demo``) AND only
        when ``AGENT_DEMO_MODE`` is enabled. A normal turn has ``demo=False``,
        so ``is_demo`` is always False on the public chat path and a fallback
        NEVER substitutes demo content — a deep_research request with CrewAI off
        degrades visibly to native instead of answering with fabricated text.
        """
        route = ctx.extra.get("route")
        multi_agent_requested = bool(getattr(route, "use_multi_agent", False))
        requested_mode = getattr(route, "requested_mode", "auto") or "auto"
        effective_mode = getattr(route, "mode", "auto") or "auto"
        profile = getattr(route, "agent_profile", None) or getattr(
            ctx, "agent_profile", None
        ) or "general"

        settings = get_settings()
        req = getattr(ctx, "request", None)
        # Two gates for demo: the env flag AND the per-request flag. Neither
        # alone is enough — that is what previously let demo leak (env default
        # True + any deep_research request).
        demo_requested = bool(getattr(settings, "AGENT_DEMO_MODE", False)) and bool(
            getattr(req, "demo", False)
        )
        # When real CrewAI is enabled, prefer it even if demo was requested;
        # demo is only the stand-in when there is no real runtime.
        is_demo = demo_requested and not bool(getattr(settings, "CREWAI_ENABLED", False))

        if not multi_agent_requested:
            # Native turn (auto / search / create / data_analysis / chat).
            selection = RuntimeSelection(
                requested_runtime="native",
                selected_runtime="native",
                available=True,
                fallback_reason=None,
                multi_agent_requested=False,
                multi_agent_executed=False,
                agent_profile=profile,
                requested_mode=requested_mode,
                effective_mode=effective_mode,
                is_demo=False,
            )
            return self._native, selection

        available, reason = self._crewai_status(demo_requested=demo_requested)
        if available:
            selection = RuntimeSelection(
                requested_runtime="crewai",
                selected_runtime="crewai",
                available=True,
                fallback_reason=None,
                multi_agent_requested=True,
                multi_agent_executed=True,
                agent_profile=profile,
                requested_mode=requested_mode,
                effective_mode=effective_mode,
                is_demo=is_demo,
            )
            return self._crewai_runtime() or self._native, selection

        # Multi-agent requested but CrewAI unavailable (and no explicit demo
        # opt-in) → explicit native fallback. We deliberately do NOT fall back
        # to demo content here: that would answer a real research question with
        # fabricated text. The fallback is visible (fallback_reason) so the UI
        # warns the user the multi-agent run did not execute.
        logger.warning(
            "multi-agent run requested (mode=%s, profile=%s) but CrewAI unavailable (%s); "
            "falling back to native with a visible fallback_reason (demo content NOT used)",
            requested_mode, profile, reason,
        )
        selection = RuntimeSelection(
            requested_runtime="crewai",
            selected_runtime="native",
            available=False,
            fallback_reason=reason,
            multi_agent_requested=True,
            multi_agent_executed=False,
            agent_profile=profile,
            requested_mode=requested_mode,
            effective_mode=effective_mode,
            is_demo=False,
        )
        return self._native, selection

    def _crewai_status(self, demo_requested: bool = False) -> tuple[bool, str | None]:
        """Return (available, fallback_reason). Cached after the first check.

        ``demo_requested`` is the per-request opt-in: demo mode counts as
        availability ONLY when both the env flag and this flag are set, so a
        normal turn (demo=False) never treats demo as a real runtime.
        """
        settings = get_settings()
        real_enabled = bool(getattr(settings, "CREWAI_ENABLED", False))
        demo_enabled = bool(getattr(settings, "AGENT_DEMO_MODE", False)) and demo_requested
        if not (real_enabled or demo_enabled):
            return False, "crewai_disabled"
        if not self._crewai_checked:
            self._crewai_checked = True
            self._crewai = self._crewai_runtime()
        if self._crewai is None:
            return False, "crewai_not_installed"
        return True, None

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
        run.output = {**(run.output or {}), **dict(evt.data)}
        if evt.kind == "done":
            # Preserve a user-initiated cancel instead of overwriting it with
            # "completed" (the runtime emits ev_done with finish_reason=cancelled).
            if evt.data.get("finish_reason") == "cancelled" or run.status == "cancelled":
                run.status = "cancelled"
            else:
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
