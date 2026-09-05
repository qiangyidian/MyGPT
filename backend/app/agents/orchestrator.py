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

Task 6 note (planner-executor-verifier engine): a general, typed, durable
workflow engine that generalizes the static graph model into a verifiable
plan->execute->verify->replan state machine now lives in
:mod:`app.agents.workflow` (``schemas`` / ``planner`` / ``executor`` /
``verifier`` / ``engine`` / ``attempts``). Its templates
(:func:`~app.agents.workflow.planner.build_plan_for_profile`) mirror the
existing ``build_*_graph`` topology for ``deep_research``,
``parallel_research``, and ``debate``, and a
:class:`~app.agents.workflow.executor.StageAdapterExecutor` delegates each
step to the existing CrewAI stage runner so the engine can drive real crews
without reimplementing them.

Routing expert multi-step turns through the new engine is INTENTIONALLY
DEFERRED: the engine is fully built and unit-tested in isolation (see
``tests/test_workflow_engine.py``, ``tests/test_workflow_replan.py``), but the
live single-turn / CrewAI debate / research paths above are unchanged so the
hard constraint (do not break existing multi-agent flows) holds. A follow-up
turn can opt a NEW profile (or a guarded flag) onto the engine without
touching the proven execution paths.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.db_mutation import (
    commit_with_rollback,
    db_mutation_scope,
    rollback_safely,
)
from app.agents.events import append_event_safe
from app.agents.persistence import persist_terminal_run
from app.agents.run_controls import drop as drop_run_control
from app.agents.run_controls import get_or_create as get_run_control
from app.agents.runtime.native_runtime import NativeChatRuntime
from app.agents.schemas import (
    AgentEvent,
    AgentTurnContext,
    BudgetExceeded,
    RuntimeKind,
    ev_done,
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
    is_demo: bool = False  # always False; kept for wire compatibility


class ChatOrchestrator:
    """Owns the AgentRun lifecycle and runtime dispatch."""

    def __init__(self) -> None:
        self._native = NativeChatRuntime()
        self._crewai: object | None = None
        self._crewai_checked = False

    # ------------------------------------------------------------------ #
    async def stream(self, ctx: AgentTurnContext) -> AsyncIterator[AgentEvent]:
        db_lock = ctx.extra.get("db_mutation_lock")
        persistence_session_factory = ctx.extra.get("persistence_session_factory")
        # Durable path: when the durable worker calls the executor, the AgentRun
        # already exists (created + enqueued by the chat API) and the worker has
        # already appended ``run.started`` when it acquired the lease. Reuse that
        # run instead of creating a duplicate, and skip the redundant event.
        durable_run_id = ctx.extra.get("durable_run_id")
        async with db_mutation_scope(db_lock):
            try:
                if durable_run_id is not None:
                    run = await self._load_durable_run(ctx, durable_run_id)
                else:
                    run = await self._create_run(ctx)
                # Register cooperative pause/instruction controls for this run.
                ctx.extra["run_control"] = get_run_control(run.id)

                # Select the runtime BEFORE emitting run_started so the event reports the
                # real runtime (native vs crewai), not the placeholder "native".
                runtime, selection = self._select_runtime(ctx)
                ctx.extra["runtime_selection"] = selection
                run.runtime = runtime.name
                run.status = "running"
                # Append the durable run.started event (Task 4) in the same
                # transaction as the status flip. Best-effort: an event-store
                # failure must never block a run from starting. The durable
                # worker already appended ``run.started`` when it acquired the
                # lease, so skip it here to avoid a duplicate.
                if durable_run_id is None:
                    await append_event_safe(
                        ctx.db,
                        run.id,
                        "run.started",
                        {
                            "runtime": runtime.name,
                            "conversation_id": str(ctx.conversation.id),
                            "message_id": str(ctx.assistant_msg.id),
                        },
                    )
                await ctx.db.commit()
            except BaseException:
                await rollback_safely(ctx.db)
                raise

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
            # Task 6b: route deep_research through the durable workflow engine
            # when the flag is on. The engine emits the SAME event vocabulary
            # the CrewAI path emits (agent_graph / agent_status / step_* /
            # token / done) so the UI works unchanged. On ANY exception the
            # engine path logs and falls through to the proven CrewAI path
            # below — the user never loses the answer.
            if self._should_route_to_engine(selection):
                try:
                    async for evt in self._run_engine_path(ctx, run):
                        if evt.kind in ("done", "error"):
                            await self._finalize_run(
                                ctx.db,
                                run,
                                evt,
                                lock=db_lock,
                                session_factory=persistence_session_factory,
                            )
                        yield evt
                        if evt.kind in ("done", "error"):
                            return
                    return  # engine handled the whole turn
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "workflow engine routing failed for run %s, "
                        "falling back to %s: %s",
                        run.id, runtime.name, exc,
                    )
                    # Reset any partial assistant content the engine attempt
                    # wrote before failing; the fallback produces its own answer.
                    try:
                        ctx.assistant_msg.content = ""
                    except Exception:  # pragma: no cover - best effort
                        pass
            async for evt in runtime.stream_turn(ctx):
                if evt.kind in ("done", "error"):
                    await self._finalize_run(
                        ctx.db,
                        run,
                        evt,
                        lock=db_lock,
                        session_factory=persistence_session_factory,
                    )
                yield evt
                if evt.kind in ("done", "error"):
                    return
        except asyncio.CancelledError:
            await self._finalize_run(
                ctx.db,
                run,
                ev_done(
                    message_id=ctx.assistant_msg.id,
                    finish_reason="cancelled",
                    usage=ctx.extra.get("usage"),
                ),
                lock=db_lock,
                session_factory=persistence_session_factory,
            )
            raise
        except Exception as exc:
            logger.exception("runtime %s crashed: %s", runtime.name, exc)
            await self._fail_run(
                ctx.db,
                run,
                str(exc),
                lock=db_lock,
                session_factory=persistence_session_factory,
            )
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
        """
        route = ctx.extra.get("route")
        multi_agent_requested = bool(getattr(route, "use_multi_agent", False))
        requested_mode = getattr(route, "requested_mode", "auto") or "auto"
        effective_mode = getattr(route, "mode", "auto") or "auto"
        profile = getattr(route, "agent_profile", None) or getattr(
            ctx, "agent_profile", None
        ) or "general"

        settings = get_settings()

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

        available, reason = self._crewai_status()
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
                is_demo=False,
            )
            return self._crewai_runtime() or self._native, selection

        # Multi-agent requested but CrewAI unavailable → explicit native
        # fallback. The fallback is visible (fallback_reason) so the UI warns
        # the user the multi-agent run did not execute.
        logger.warning(
            "multi-agent run requested (mode=%s, profile=%s) but CrewAI unavailable (%s); "
            "falling back to native with a visible fallback_reason",
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

    # ------------------------------------------------------------------ #
    # Task 6b: workflow-engine routing for deep_research
    # ------------------------------------------------------------------ #
    def _should_route_to_engine(self, selection: RuntimeSelection) -> bool:
        """True only when the engine flag is truthy AND this is a genuine
        deep_research multi-agent turn.

        Scoped to ``deep_research`` (the simplest sequential profile) so the
        engine proves itself on one profile before generalizing.
        ``parallel_research`` / ``debate`` can be added later behind the same
        flag. When this returns False the existing path runs unchanged.
        """
        if not _truthy(getattr(get_settings(), "AGENT_WORKFLOW_ENGINE", "")):
            return False
        return (
            selection.multi_agent_requested
            and selection.agent_profile == "deep_research"
        )

    async def _run_engine_path(
        self, ctx: AgentTurnContext, run: AgentRun
    ) -> AsyncIterator[AgentEvent]:
        """Run one deep_research turn through the durable WorkflowEngine.

        Emits the SAME event vocabulary the CrewAI multi-agent path emits
        (``agent_graph`` once, then ``agent_status`` / ``step_started`` /
        ``step_completed`` per step, then ``token`` + ``done``) so the frontend
        works unchanged. Reuses :class:`StageAdapterExecutor` so each step
        delegates to the existing :class:`CrewAIStageExecutor` — no CrewAI
        reimplementation.

        Raises on ANY failure (engine exception or non-completed result) so the
        caller falls back to the proven CrewAI path. Engine events already
        emitted are kept (the fallback re-emits its own ``agent_graph`` which
        resets the panel).

        A test (or any caller) may inject an executor via
        ``ctx.extra["workflow_executor"]`` to bypass the real CrewAI stages
        (mirrors the CrewAI runtime's ``ctx.extra["stage_executor"]`` seam).
        """
        # Late imports: keep crewai/workflow out of the module-load path.
        from app.agents.graph import build_deep_research_graph
        from app.agents.schemas import (
            ev_agent_graph,
            ev_agent_status,
            ev_done,
            ev_run_status,
            ev_step_completed,
            ev_step_started,
            ev_token,
        )
        from app.agents.token_budget import PromptAdmissionError
        from app.agents.workflow.engine import WorkflowEngine
        from app.agents.workflow.planner import build_deep_research_plan
        from app.agents.workflow.schemas import StepError
        from app.agents.workflow.verifier import RuleBasedVerifier

        question = ctx.user_content or ""
        plan = build_deep_research_plan(question)
        # Reuse the static deep_research topology for the agent_graph event
        # (the SAME graph build_research_stages would produce). This does not
        # import crewai, so the engine path is unit-testable without it.
        graph = build_deep_research_graph(question)
        graph.run_id = str(run.id)

        yield ev_agent_graph(run_id=run.id, graph=graph.to_public_dict())
        yield ev_run_status(run_id=run.id, status="running", current_agent_ids=[])

        # Resolve the per-step executor. An injected executor (tests / future
        # wiring) wins; otherwise build the real StageAdapterExecutor from the
        # existing crew's stage specs so each step runs via CrewAIStageExecutor.
        injected = ctx.extra.get("workflow_executor")
        if injected is not None:
            inner = injected
        else:
            inner = self._build_stage_adapter(ctx, run)

        # The engine calls executor.execute(step, upstream) for each ready step.
        # Wrap it to (a) surface the lifecycle as SSE events the frontend
        # already understands, and (b) map Budget/PromptAdmission errors to
        # StepError(transient=False) so the engine fails the step immediately
        # instead of retrying a hard budget exhaustion.
        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()

        class _EmittingExecutor:
            async def execute(self, step: Any, upstream: dict) -> Any:
                title = (step.task_description or step.name or step.id)[:80]
                await queue.put(
                    ev_step_started(
                        step_id=step.id, title=title, step_type="llm",
                        agent=step.role or step.id,
                    )
                )
                await queue.put(
                    ev_agent_status(
                        run_id=run.id, agent_id=step.id, status="running",
                        task_title=title,
                    )
                )
                try:
                    obs = await inner.execute(step, upstream)
                except (BudgetExceeded, PromptAdmissionError) as exc:
                    # Hard budget/admission exhaustion — never retry.
                    await queue.put(
                        ev_agent_status(
                            run_id=run.id, agent_id=step.id, status="failed",
                            error=str(exc),
                        )
                    )
                    raise StepError(str(exc), transient=False) from exc
                except Exception as exc:
                    await queue.put(
                        ev_agent_status(
                            run_id=run.id, agent_id=step.id, status="failed",
                            error=str(exc),
                        )
                    )
                    raise
                await queue.put(ev_step_completed(step_id=step.id, status="done"))
                await queue.put(
                    ev_agent_status(
                        run_id=run.id, agent_id=step.id, status="completed",
                        output_summary=(obs.output or "")[:160] or None,
                    )
                )
                return obs

        engine = WorkflowEngine(
            executor=_EmittingExecutor(),
            verifier=RuleBasedVerifier(),
            run_id=run.id,
            session_factory=ctx.extra.get("persistence_session_factory"),
        )

        # Drive the engine in a task; concurrently drain its lifecycle events
        # so the UI updates in real time (same pattern as CrewAIRuntime's
        # run_flow + queue drain).
        result_holder: dict[str, Any] = {}
        engine_exc: list[BaseException] = []

        async def _run_engine() -> None:
            try:
                result_holder["result"] = await engine.run(plan)
            except BaseException as exc:
                engine_exc.append(exc)
            finally:
                await queue.put(None)

        engine_task = asyncio.create_task(_run_engine())
        try:
            while True:
                evt = await queue.get()
                if evt is None:
                    break
                yield evt
        finally:
            if not engine_task.done():
                engine_task.cancel()
            try:
                await engine_task
            except (asyncio.CancelledError, Exception):
                pass

        if engine_exc:
            raise engine_exc[0]

        result = result_holder.get("result")
        if result is None or result.status != "completed":
            # A non-completed result (e.g. a step failed permanently) MUST
            # trigger the fallback so the user still gets the CrewAI answer.
            raise RuntimeError(
                f"workflow engine did not complete (status="
                f"{getattr(result, 'status', 'missing')}, "
                f"error={getattr(result, 'error', None)})"
            )

        yield ev_run_status(
            run_id=run.id, status="completed", current_agent_ids=[]
        )

        # The writer step holds the final cited answer.
        writer_obs = result.observations.get("writer")
        final_text = (writer_obs.output if writer_obs else "") or ""
        ctx.assistant_msg.content = final_text
        if final_text:
            yield ev_token(delta=final_text)
        yield ev_done(message_id=ctx.assistant_msg.id, finish_reason="stop")

    def _build_stage_adapter(self, ctx: AgentTurnContext, run: AgentRun):
        """Build the real StageAdapterExecutor from the existing crew stages.

        Imports crewai lazily (via the crew builder) and reuses the runtime's
        LLM/tool/stage-context construction so each engine step runs through
        the SAME CrewAIStageExecutor the live CrewAI path uses.
        """
        from app.agents.adapters.llm_adapter import CrewAILLMFactory
        from app.agents.crews import build_research_stages
        from app.agents.runtime.crewai_runtime import _guard_for_context
        from app.agents.stage_context import make_stage_context
        from app.agents.workflow.executor import StageAdapterExecutor

        guard = _guard_for_context(ctx)
        llm = CrewAILLMFactory.from_model_config(ctx.model_config, budget_guard=guard)
        stage_ctx = make_stage_context(str(run.id), budget_guard=guard)
        # tools are not strictly needed by the adapter contract (CrewAIStageExecutor
        # receives agent+task from the StageSpec, which already embed tools); pass
        # an empty list to satisfy the builder signature.
        _, stages = build_research_stages(
            llm=llm, tools=[], question=ctx.user_content or ""
        )
        stages_by_id = {spec.agent_id: spec for spec in stages}
        return StageAdapterExecutor(stages_by_id, stage_ctx)

    def _crewai_status(self) -> tuple[bool, str | None]:
        """Return (available, fallback_reason). Cached after the first check."""
        settings = get_settings()
        if not bool(getattr(settings, "CREWAI_ENABLED", False)):
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
        except Exception as exc:
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
            started_at=datetime.now(UTC),
        )
        ctx.db.add(run)
        await ctx.db.flush()
        ctx.run_id = run.id
        ctx.extra["run_id"] = run.id
        return run

    async def _load_durable_run(
        self, ctx: AgentTurnContext, run_id: uuid.UUID | str
    ) -> AgentRun:
        """Load an existing durable run instead of creating a new one.

        The durable worker creates the AgentRun (and acquires a lease) before
        calling the executor. The orchestrator reuses that run row instead of
        creating a duplicate, and wires ``ctx.run_id`` to it. Falls back to
        :meth:`_create_run` defensively if the run vanished mid-flight.
        """
        run = await ctx.db.get(AgentRun, run_id)
        if run is None:
            return await self._create_run(ctx)
        ctx.run_id = run.id
        ctx.extra["run_id"] = run.id
        return run

    async def _finalize_run(
        self,
        db: AsyncSession,
        run: AgentRun,
        evt: AgentEvent,
        *,
        lock: asyncio.Lock | None = None,
        session_factory: Any = None,
    ) -> None:
        if session_factory is not None:
            try:
                await persist_terminal_run(
                    session_factory,
                    run_id=run.id,
                    event_kind=evt.kind,
                    event_data=dict(evt.data),
                )
            except Exception:  # pragma: no cover - best effort
                logger.exception("failed to finalize agent_run %s", run.id)
            # Best-effort durable terminal event on a fresh session so the
            # event log matches the persisted terminal status.
            terminal_type = (
                "run.cancelled"
                if evt.kind == "done"
                and (
                    evt.data.get("finish_reason") == "cancelled"
                    or getattr(run, "status", None) == "cancelled"
                )
                else ("run.completed" if evt.kind == "done" else "run.failed")
            )
            try:
                async with session_factory() as sess:
                    await append_event_safe(
                        sess,
                        run.id,
                        terminal_type,
                        {
                            "finish_reason": evt.data.get("finish_reason"),
                            "message": evt.data.get("message", ""),
                        },
                    )
                    await sess.commit()
            except Exception:  # pragma: no cover - best effort
                logger.debug(
                    "terminal event append failed for run %s", run.id, exc_info=True
                )
            return
        run.finished_at = datetime.now(UTC)
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
        # Best-effort durable terminal event in the same transaction.
        await append_event_safe(
            db,
            run.id,
            "run.cancelled"
            if run.status == "cancelled"
            else ("run.completed" if run.status == "completed" else "run.failed"),
            {
                "finish_reason": evt.data.get("finish_reason"),
                "message": evt.data.get("message", ""),
            },
        )
        async with db_mutation_scope(lock):
            try:
                await commit_with_rollback(db)
            except Exception:  # pragma: no cover - best effort
                logger.exception("failed to finalize agent_run %s", run.id)

    async def _fail_run(
        self,
        db: AsyncSession,
        run: AgentRun,
        message: str,
        *,
        lock: asyncio.Lock | None = None,
        session_factory: Any = None,
    ) -> None:
        if session_factory is not None:
            try:
                await persist_terminal_run(
                    session_factory,
                    run_id=run.id,
                    event_kind="error",
                    event_data={"message": message},
                )
            except Exception:  # pragma: no cover
                pass
            # Best-effort durable terminal event on a fresh session.
            try:
                async with session_factory() as sess:
                    await append_event_safe(
                        sess, run.id, "run.failed", {"message": message}
                    )
                    await sess.commit()
            except Exception:  # pragma: no cover - best effort
                logger.debug(
                    "terminal event append failed for run %s", run.id, exc_info=True
                )
            return
        run.finished_at = datetime.now(UTC)
        run.status = "failed"
        run.error_message = message
        await append_event_safe(db, run.id, "run.failed", {"message": message})
        async with db_mutation_scope(lock):
            try:
                await commit_with_rollback(db)
            except Exception:  # pragma: no cover
                pass


# Module-level singleton — stateless aside from the lazy crewai cache.
chat_orchestrator = ChatOrchestrator()


def _truthy(value: str | None) -> bool:
    """Interpret a settings flag string as a boolean (1/true/yes/on -> True)."""
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")
