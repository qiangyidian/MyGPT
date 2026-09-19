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
from app.agents.graph import graph_from_plan
from app.agents.run_environment import RunEnvironment
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
from app.db import AsyncSessionLocal
from app.models import AgentRun
from app.observability import observe_counter

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
        # The concrete import error when the CrewAI runtime fails to load.
        # Surfaced via runtime_selected.fallback_reason and the admin
        # agent-runtime diagnostics endpoint so a server-side import failure
        # (missing wheel, dependency conflict) is diagnosable from the UI
        # instead of collapsing into an opaque "crewai_not_installed".
        self._crewai_import_error: str | None = None

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
    # Task 6b: workflow-engine routing (profile roster)
    # ------------------------------------------------------------------ #
    def _should_route_to_engine(self, selection: RuntimeSelection) -> bool:
        """True 当且仅当：总开关为真、本回合真的要多 Agent、且 profile 在名单内。

        名单为空 → 一律不走引擎（安全默认）。每个 profile 可单独摘除，
        摘除后立刻回到已验证的 CrewAI 路径。
        """
        settings = get_settings()
        if not _truthy(getattr(settings, "AGENT_WORKFLOW_ENGINE", "")):
            return False
        if not selection.multi_agent_requested:
            return False
        if selection.agent_profile not in _engine_profiles(settings):
            return False
        # 灰度面指标：只在引擎真正接管时发，profile 维度可量化。
        observe_counter("agent.engine.profile", 1, profile=selection.agent_profile)
        return True

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
        from app.agents.schemas import (
            ev_done,
            ev_token,
        )
        from app.agents.token_budget import PromptAdmissionError
        from app.agents.workflow.engine import WorkflowEngine
        from app.agents.workflow.planner import build_plan_for_profile
        from app.agents.workflow.schemas import StepError
        from app.agents.workflow.verifier import RuleBasedVerifier

        question = ctx.user_content or ""
        # profile 的唯一权威来源是 RuntimeSelection（orchestrator 在 stream()
        # 里写进 ctx.extra）。**不要**用 run.flow_name —— 那一列在 _create_run
        # 里写死成 "native_chat"，引擎路径从不更新它，用它会让所有 profile
        # 静默退化成 deep_research。
        selection = ctx.extra.get("runtime_selection")
        profile = getattr(selection, "agent_profile", None) or "deep_research"
        plan = build_plan_for_profile(profile, question)
        # 拓扑由 plan 推导 —— 与 walker 的静态 builder 等价（test_graph_from_plan
        # 对三个 profile 都有断言）。不再 import 具体的 builder，否则每加一个
        # profile 都要改这里。
        graph = graph_from_plan(plan)

        env = RunEnvironment.for_turn(ctx)
        env.attach_graph(graph)
        # 计划门（与 walker 同语义）：计划先行、默认不阻塞；只有用户主动
        # 上闸（RunControl.request_gate）才在这里等确认。
        if bool(getattr(get_settings(), "PLAN_REQUIRE_CONFIRMATION", False)):

            async def _plan_status() -> str | None:
                from sqlalchemy import select

                factory = (
                    ctx.extra.get("persistence_session_factory")
                    or AsyncSessionLocal
                )
                async with db_mutation_scope(ctx.extra.get("persistence_lock")):
                    async with factory() as session:
                        row = await session.execute(
                            select(AgentRun.plan_status).where(AgentRun.id == run.id)
                        )
                        return row.scalar_one_or_none()

            gated = await env.await_plan_confirmation(_plan_status)
            if not gated:
                logger.warning(
                    "plan confirmation timed out for run %s; proceeding", run.id
                )
        env.begin()
        # 图定义只落一次，与 walker 路径一致（刷新后可恢复链路）。
        await env.persist_graph(definition=True)

        injected = ctx.extra.get("workflow_executor")
        if injected is not None:
            inner = injected
        else:
            inner = self._build_stage_adapter(ctx, run, env)

        class _EnvExecutor:
            """把引擎的步骤执行接到 RunEnvironment 的共享上下文上。

            只做一件事：把硬预算/准入失败映射成 StepError(transient=False)，
            让引擎立即失败而不是重试一个已经耗尽的预算。事件发射全部交给
            on_step_* 回调（见下），这里不再自造事件。
            """

            async def execute(self, step: Any, upstream: dict) -> Any:
                try:
                    return await inner.execute(step, upstream)
                except (BudgetExceeded, PromptAdmissionError) as exc:
                    raise StepError(str(exc), transient=False) from exc

        engine = WorkflowEngine(
            executor=_EnvExecutor(),
            verifier=RuleBasedVerifier(),
            run_id=run.id,
            session_factory=ctx.extra.get("persistence_session_factory"),
            before_step=lambda step_id: env.respect_controls(),
            on_step_start=env.step_started,
            on_step_end=lambda step_id, output, usage: env.step_completed(
                step_id,
                output=output,
                output_summary=(output or "")[:160] or None,
                usage=usage,
            ),
            on_step_error=lambda step_id, message: env.step_failed(
                step_id, error=message
            ),
        )

        # 驱动引擎的同时并发排空 env 的事件队列，让面板实时更新 —— 与 walker
        # 路径的 run_flow + drain 同构。若写成 `result = await engine.run(plan)`
        # 再排空，所有过程事件会挤到最后一次性到达，面板在整个多 Agent 轮次
        # 里全程静止。
        #
        # 哨兵（None）由引擎任务在 finally 里投放，保证它排在所有步骤事件之后：
        # stage_ctx.emit 走 call_soon_threadsafe，回调要到下一个 loop tick 才
        # 执行；先 await asyncio.sleep(0) 让已排队的回调落地，再放哨兵。
        result_holder: dict[str, Any] = {}
        engine_exc: list[BaseException] = []

        async def _run_engine() -> None:
            try:
                result_holder["result"] = await engine.run(plan)
            except BaseException as exc:
                engine_exc.append(exc)
            finally:
                await asyncio.sleep(0)
                env.stage_ctx.close()

        engine_task = asyncio.create_task(_run_engine())
        try:
            while True:
                evt = await env.stage_ctx.queue.get()
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

        env.finish("completed")
        await asyncio.sleep(0)  # 让 finish 的事件落地（同上）
        for evt in _drain_env_events(env):
            yield evt
        # 终态快照。必须在这里：env.finish() 在 drain 循环之后才把图翻到
        # completed，所以循环内的任何落库都不可能包含终态。引擎任务此时已
        # join，写入都已提交，不再与它争连接。
        await env.persist_graph(definition=False)

        # 终态步骤 = 拓扑序最后一个。模板里它是 writer，但这里**不按名字硬编码**
        # —— 拓扑已由 plan 决定，名字硬编码会在新 profile 上静默取错。
        terminal_step = plan.topological_order()[-1]
        terminal_obs = result.observations.get(terminal_step)
        final_text = (terminal_obs.output if terminal_obs else "") or ""
        ctx.assistant_msg.content = final_text
        if final_text:
            yield ev_token(delta=final_text)

        usage = env.aggregate_usage(result.observations)
        if usage:
            ctx.extra["usage"] = usage
        ctx.extra["finish_reason"] = "stop"
        yield ev_done(
            message_id=ctx.assistant_msg.id,
            finish_reason="stop",
            usage=usage,
            budget=ctx.extra.get("budget"),
        )

    def _build_stage_adapter(
        self, ctx: AgentTurnContext, run: AgentRun, env: RunEnvironment
    ):
        """Build the real StageAdapterExecutor from the existing crew stages.

        Imports crewai lazily (via the crew builder) and reuses the runtime's
        LLM/tool/stage-context construction so each engine step runs through
        the SAME CrewAIStageExecutor the live CrewAI path uses.
        """
        from app.agents.adapters.llm_adapter import CrewAILLMFactory
        from app.agents.crews import (
            build_debate_stages,
            build_parallel_research_stages,
            build_research_stages,
            build_task_decomposition_stages,
        )
        from app.agents.workflow.executor import StageAdapterExecutor

        # stage_ctx / 预算守卫来自共享的 RunEnvironment —— 与 walker 路径同一实例。
        guard = env.guard
        llm = CrewAILLMFactory.from_model_config(ctx.model_config, budget_guard=guard)
        stage_ctx = env.stage_ctx
        # tools are not strictly needed by the adapter contract (CrewAIStageExecutor
        # receives agent+task from the StageSpec, which already embed tools); pass
        # an empty list to satisfy the builder signature.
        builders = {
            "parallel_research": build_parallel_research_stages,
            "debate": build_debate_stages,
            "task_decomposition": build_task_decomposition_stages,
        }
        # 与 plan 用同一个 profile 来源（见上）—— 两处必须一致，否则 plan 里
        # 的 step id 与这里取出的 stage 对不上，直接 KeyError。
        selection = ctx.extra.get("runtime_selection")
        profile = getattr(selection, "agent_profile", None) or "deep_research"
        builder = builders.get(profile, build_research_stages)
        _, stages = builder(llm=llm, tools=[], question=ctx.user_content or "")
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
            reason = "crewai_not_installed"
            if self._crewai_import_error:
                reason = f"crewai_not_installed ({self._crewai_import_error})"
            return False, reason
        return True, None

    def _crewai_runtime(self):  # pragma: no cover - implemented in Phase 1
        """Lazily build the CrewAI runtime. Returns None if crewai isn't importable."""
        try:
            from app.agents.runtime.crewai_runtime import CrewAIRuntime  # type: ignore
        except Exception as exc:
            # Keep the concrete error: "crewai_not_installed" alone hides WHY
            # (ModuleNotFoundError vs a dependency conflict) and makes a prod
            # fallback undiagnosable without shell access.
            self._crewai_import_error = f"{type(exc).__name__}: {exc}"[:300]
            logger.warning(
                "CrewAI runtime unavailable, falling back to native: %s",
                self._crewai_import_error,
            )
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


def _drain_env_events(env: RunEnvironment) -> list[AgentEvent]:
    """取出 RunEnvironment 队列中累积的事件（非阻塞）。"""
    out: list[AgentEvent] = []
    while not env.stage_ctx.queue.empty():
        evt = env.stage_ctx.queue.get_nowait()
        if evt is not None:
            out.append(evt)
    return out


def _engine_profiles(settings: Any) -> frozenset[str]:
    """解析 profile 名单（逗号分隔，容忍空白与空项）。"""
    raw = getattr(settings, "AGENT_WORKFLOW_ENGINE_PROFILES", "") or ""
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def _truthy(value: str | None) -> bool:
    """Interpret a settings flag string as a boolean (1/true/yes/on -> True)."""
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")
