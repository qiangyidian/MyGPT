"""CrewAI runtime: real multi-agent lifecycle via explicit per-stage orchestration.

Why explicit orchestration (not the CrewAI event bus): the bus routes events by
source and could not be made to fire reliably for per-agent task lifecycle
without a live kickoff context; relying on it as the *only* source of truth
risks faking state. Instead each agent runs via ``Agent.aexecute_task`` and the
runtime emits ``agent_started`` / ``agent_completed`` (or ``failed``) around the
real call — so at any instant the running set reflects actual execution.

Flow (multi-agent profiles: deep_research, parallel_research):

  1. Build the static :class:`AgentGraph` + stage specs.
  2. Push an ``agent_graph`` event (full topology) — opens the right-side panel.
  3. Run stages grouped by ``stage`` number: same-stage specs with no
     inter-dependency run concurrently via ``asyncio.gather`` (genuine
     parallelism — multiple agents ``running`` at once). Join nodes (analyst)
     only run once all predecessor edges are completed.
  4. Tool calls executed inside ``aexecute_task`` are attributed to the current
     agent (shared :class:`StageContext`) and forwarded as ``tool_call`` /
     ``tool_result`` events in real time via a thread-safe queue.
  5. The final stage's raw output is emitted as the answer tokens.

A ``StageExecutor`` abstraction makes the lifecycle unit-testable without a
live LLM (tests inject a :class:`FakeStageExecutor` via ``ctx.extra``).
``crewai`` is imported lazily so the app boots without it.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from types import SimpleNamespace
from typing import Any, AsyncIterator

from sqlalchemy import select

from app.agents.approval_bridge import ApprovalBridge
from app.agents.adapters.llm_adapter import CrewAILLMFactory
from app.agents.adapters.tool_adapter import build_crewai_tool
from app.agents.continuation import aggregate_usage
from app.agents.db_mutation import db_mutation_scope
from app.agents.events import append_event_safe
from app.agents.persistence import persist_graph_snapshot, persist_research_plan
from app.agents.crews import (
    build_debate_stages,
    build_parallel_research_stages,
    build_research_stages,
)
from app.agents.crews.stage import StageSpec
from app.agents.graph import AgentGraph
from app.agents.lifecycle import AgentLifecycleEmitter
from app.agents.planning import build_plan, classify_intent
from app.agents.runtime.stage_executor import (
    CrewAIStageExecutor,
    DemoStageExecutor,
    FakeStageExecutor,
    StageExecutor,
    StageResult,
    _extract_usage_rounds,
)
from app.agents.policies import BudgetExceeded, BudgetGuard, BudgetLimits
from app.agents.schemas import (
    AgentEvent,
    AgentTurnContext,
    ev_done,
    ev_error,
    ev_plan_created,
    ev_research_plan,
    ev_run_instruction_received,
    ev_run_paused,
    ev_run_resumed,
    ev_token,
)
from app.agents.run_controls import get as get_run_control
from app.agents.stage_context import StageContext, make_stage_context
from app.agents.streaming_writer import StreamingWriterExecutor
from app.agents.token_budget import PromptAdmissionError
from app.core.config import get_settings
from app.core.pricing import usage_cost
from app.db import AsyncSessionLocal
from app.models import AgentRun
from app.providers.base import PROVIDER_ERR_TIMEOUT, ProviderError
from app.providers.registry import get_provider_for_config

logger = logging.getLogger(__name__)


def _acceptance_criteria_for(question: str, plan_steps: list[dict[str, Any]]) -> list[str]:
    """Deterministic, reviewable acceptance criteria for a research plan.

    Derived from the plan steps (not the free-form question) so the criteria
    always match what will actually run; the frontend renders these in the
    PlanReview card before the user confirms.
    """
    criteria = [
        "回答直接针对用户问题，不偏题",
        "关键结论附带可核实的来源引用",
    ]
    n_steps = len(plan_steps or [])
    if n_steps:
        criteria.append(f"完成全部 {n_steps} 个计划步骤（检索、交叉核对、撰写）")
    if question and len(question) > 30:
        criteria.append("对问题中的多个子点分别作答，不遗漏")
    criteria.append("明确区分事实与推断；无法核实的结论标注不确定性")
    return criteria

# Profiles that use the multi-agent graph + right-side panel.
_MULTI_AGENT_PROFILES = {"deep_research", "parallel_research", "debate"}


def _guard_for_context(ctx: Any) -> BudgetGuard:
    """Resolve/inject the run guard, tolerating lightweight protocol fakes."""
    guard = getattr(ctx, "budget_guard", None)
    if guard is not None:
        return guard
    extra = getattr(ctx, "extra", {}) or {}
    guard = BudgetGuard(
        BudgetLimits.from_settings(
            get_settings(),
            extra.get("budget_overrides"),
            allow_increase=bool(extra.get("budget_policy_authorized", False)),
        )
    )
    setattr(ctx, "budget_guard", guard)
    return guard


def _demo_executor_enabled(settings: Any, request: Any) -> bool:
    """Decide whether the deterministic DemoStageExecutor should run this turn.

    Demo is a STRICT double opt-in: the env flag (AGENT_DEMO_MODE) AND the
    per-request flag (request.demo) must both be set, AND no real CrewAI
    runtime may be configured (CREWAI_ENABLED) — real CrewAI always wins. This
    MUST stay in lock-step with ChatOrchestrator's ``is_demo`` computation so the
    ``runtime_selected`` event's is_demo (which drives the UI warning banner)
    matches the executor that actually ran. Diverging here would serve canned
    demo content with no visible warning — the exact regression this guards.
    """
    return (
        bool(getattr(settings, "AGENT_DEMO_MODE", False))
        and bool(getattr(request, "demo", False))
        and not bool(getattr(settings, "CREWAI_ENABLED", False))
    )


def _writer_finish_reason(stages: list[StageSpec], outputs: dict[str, StageResult]) -> str:
    """Pull the writer stage's REAL finish_reason out of its StageResult.

    The writer records the upstream reason (length/stop/cancelled/...) in
    ``StageResult.structured["finish_reason"]``; default to ``stop`` only when no
    writer result exists. This replaces the old hard-coded ``stop`` that clobbered
    a real ``length`` truncation.
    """
    for spec in stages:
        if getattr(spec, "agent_id", "") == "writer":
            res = outputs.get(spec.agent_id)
            if res and isinstance(res.structured, dict):
                fr = res.structured.get("finish_reason")
                if fr:
                    return fr
            break
    return "stop"


def _writer_usage(
    stages: list[StageSpec], outputs: dict[str, StageResult]
) -> dict[str, Any] | None:
    """Return the writer's already-aggregated provider usage, when available."""
    for spec in stages:
        if getattr(spec, "agent_id", "") != "writer":
            continue
        result = outputs.get(spec.agent_id)
        if result and result.usage:
            return result.usage
        if result and isinstance(result.structured, dict):
            usage = result.structured.get("usage")
            if isinstance(usage, dict) and usage:
                return usage
        break
    return None


def _aggregate_crewai_usage(
    stages: list[StageSpec],
    outputs: dict[str, StageResult],
    stage_ctx: StageContext,
) -> dict[str, int | float] | None:
    """Aggregate each stage plus idempotently recorded tool/failed-attempt usage."""
    rounds: list[dict[str, Any] | None] = []
    directly_charged_prefixes: list[str] = []
    for spec in stages:
        result = outputs.get(getattr(spec, "agent_id", ""))
        if result is None:
            continue
        if result.usage:
            rounds.append(result.usage)
            if result.usage_charged:
                directly_charged_prefixes.append(
                    f"model:{getattr(spec, 'agent_id', '')}:"
                )
        elif isinstance(result.structured, dict):
            rounds.append(result.structured.get("usage"))
    rounds.extend(
        usage
        for key, usage in stage_ctx.usage_records.items()
        if not any(key.startswith(prefix) for prefix in directly_charged_prefixes)
    )
    return aggregate_usage(rounds)


def _map_run_error(exc: Exception) -> tuple[str, str]:
    """Map a multi-agent flow exception to (finish_reason, ev_error code)."""
    if isinstance(exc, PromptAdmissionError):
        return "budget", exc.code
    if isinstance(exc, BudgetExceeded):
        return "budget", "agent_budget_exceeded"
    if isinstance(exc, ProviderError):
        if getattr(exc, "code", "") == PROVIDER_ERR_TIMEOUT:
            return "timeout", "provider_timeout"
        return "provider_error", "provider_error"
    return "error", "crewai_run_error"


async def _single_llm_usage_snapshot(llm: Any) -> dict[str, int | float] | None:
    summary = getattr(llm, "get_token_usage_summary", None)
    if not callable(summary):
        return None
    try:
        value = summary()
        if inspect.isawaitable(value):
            value = await value
        return aggregate_usage(_extract_usage_rounds(SimpleNamespace(usage=value)))
    except Exception:  # usage must never mask the model outcome
        logger.debug("single CrewAI usage snapshot failed", exc_info=True)
        return None


def _nonnegative_usage_delta(
    before: dict[str, int | float] | None,
    after: dict[str, int | float] | None,
) -> dict[str, int | float] | None:
    if not after:
        return None
    baseline = before or {}
    delta = {
        key: value - baseline.get(key, 0)
        for key, value in after.items()
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value - baseline.get(key, 0) > 0
    }
    return delta or None


_CREWAI_BODY_SANITIZER_INSTALLED = False


def _coerce_chat_body(content: bytes) -> bytes | None:
    """Rewrite a chat-completions body so no assistant message has content=null.

    CrewAI resends an assistant tool-call message with ``content: null`` (the
    OpenAI convention when the model only emits tool_calls). Anthropic-strict
    OpenAI-compatible gateways (e.g. the GLM proxy) reject ``null`` content
    after extracting the system message, 422-ing a tool-calling agent on its
    second turn. The native runtime already coerces this (``"".join(...) or
    ""``); this applies the same fix to CrewAI's path. Returns the rewritten
    bytes, or None if the body was unchanged / not a chat body.
    """
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        return None
    msgs = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(msgs, list):
        return None
    changed = False
    for m in msgs:
        if (
            isinstance(m, dict)
            and m.get("role") == "assistant"
            and m.get("content") is None
        ):
            m["content"] = ""
            changed = True
    if not changed:
        return None
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _install_chat_body_sanitizer() -> None:
    """Install a sync-httpx hook that runs :func:`_coerce_chat_body` on every
    outgoing request body. Idempotent; no-ops on non-chat HTTP, so it is safe
    to leave installed for the process lifetime. CrewAI runs its openai client
    in an executor (sync httpx), so patching ``httpx.Client.send`` is enough.
    """
    global _CREWAI_BODY_SANITIZER_INSTALLED
    if _CREWAI_BODY_SANITIZER_INSTALLED:
        return
    import httpx

    _orig_sync = httpx.Client.send

    def _send_sync(self, request, *args, **kwargs):
        content = getattr(request, "content", None)
        if content:
            new = _coerce_chat_body(content)
            if new is not None:
                request._content = new
        return _orig_sync(self, request, *args, **kwargs)

    httpx.Client.send = _send_sync  # type: ignore[assignment]
    _CREWAI_BODY_SANITIZER_INSTALLED = True


class CrewAIRuntime:
    """Multi-agent CrewAI runtime. Implements the :class:`AgentRuntime` protocol."""

    name = "crewai"

    async def stream_turn(self, ctx: AgentTurnContext) -> AsyncIterator[AgentEvent]:
        """Thin wrapper: guarantees per-tenant connector sessions (opened in
        :meth:`_build_tools`) are torn down at run end (graceful shutdown)."""
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
                        "crewai connector session close failed for run %s",
                        ctx.run_id, exc_info=True,
                    )

    async def _stream_turn_body(
        self, ctx: AgentTurnContext
    ) -> AsyncIterator[AgentEvent]:
        # Coerce null assistant content on CrewAI's outgoing chat body so
        # Anthropic-strict OpenAI-compatible gateways (GLM proxy) don't 422.
        _install_chat_body_sanitizer()
        guard = _guard_for_context(ctx)
        # 1. LLM from the existing ModelConfig.
        try:
            llm = CrewAILLMFactory.from_model_config(
                ctx.model_config, budget_guard=guard
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("crewai LLM build failed: %s", exc)
            yield ev_error(code="crewai_llm_error", message=str(exc))
            return

        intent = classify_intent(ctx.user_content) if ctx.enable_tools else "chat"
        ctx.extra["intent"] = intent
        profile = ctx.agent_profile

        # Decide single- vs multi-agent.
        use_multi = ctx.enable_tools and (
            profile in _MULTI_AGENT_PROFILES or intent == "deep_research"
        )

        if not use_multi:
            # Single-agent path: keep the lightweight plan + one Crew kickoff.
            # No agent_graph → the right-side panel stays closed; the in-bubble
            # 执行过程 (ResearchSteps) handles the trace via plan/step events.
            async for evt in self._run_single_agent(ctx, llm, intent):
                yield evt
            return

        # ---- multi-agent path ----
        async for evt in self._run_multi_agent(ctx, llm, profile or "deep_research"):
            yield evt

    # ====================================================================== #
    # Single-agent path (unchanged behaviour, lightweight)
    # ====================================================================== #
    async def _run_single_agent(
        self, ctx: AgentTurnContext, llm: Any, intent: str
    ) -> AsyncIterator[AgentEvent]:
        from crewai import Agent, Crew, Process, Task

        plan_summary, plan_steps = build_plan(intent, ctx.user_content)
        yield ev_plan_created(summary=plan_summary, steps=plan_steps)

        guard = _guard_for_context(ctx)
        tools = await self._build_tools(ctx, stage_ctx=None)
        try:
            agent = Agent(
                role="Assistant",
                goal="Answer the user's request accurately, using tools when needed.",
                backstory="A helpful, concise assistant.",
                llm=llm,
                tools=tools or None,
                allow_delegation=False,
                verbose=False,
            )
            task = Task(
                description=ctx.user_content or "Answer the user.",
                expected_output="A concise, well-structured answer.",
                agent=agent,
            )
            crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, memory=False, verbose=False)
        except Exception as exc:  # noqa: BLE001
            logger.exception("crewai setup failed: %s", exc)
            yield ev_error(code="crewai_setup_error", message=str(exc))
            return

        usage_before = await _single_llm_usage_snapshot(agent.llm)
        realtime_generation_before = int(
            getattr(agent.llm, "_usage_charge_generation", 0) or 0
        )

        def realtime_usage_charged() -> bool:
            return (
                int(getattr(agent.llm, "_usage_charge_generation", 0) or 0)
                > realtime_generation_before
            )

        metered_usage: dict[str, int | float] | None = None
        try:
            guard.check()
            try:
                try:
                    async with asyncio.timeout(guard.remaining_seconds):
                        result = await crew.kickoff_async()
                finally:
                    usage_after = await _single_llm_usage_snapshot(agent.llm)
                    metered_usage = _nonnegative_usage_delta(
                        usage_before, usage_after
                    )
                    if metered_usage:
                        cost = metered_usage.get("cost_usd")
                        if cost is None:
                            cost = usage_cost(
                                getattr(
                                    getattr(ctx, "model_config", None),
                                    "model_name",
                                    None,
                                ),
                                metered_usage,
                            )
                        if not realtime_usage_charged():
                            guard.add_usage(
                                metered_usage,
                                cost_usd=cost,
                                usage_id="crewai:single:cumulative",
                            )
                        ctx.extra["usage"] = metered_usage
            except TimeoutError as exc:
                raise BudgetExceeded(
                    f"time budget ({guard.limits.max_runtime_seconds}s) exceeded"
                ) from exc
            usage = metered_usage or aggregate_usage(_extract_usage_rounds(result))
            if usage and metered_usage is None:
                cost = usage.get("cost_usd")
                if cost is None:
                    cost = usage_cost(
                        getattr(getattr(ctx, "model_config", None), "model_name", None),
                        usage,
                    )
                if not realtime_usage_charged():
                    guard.add_usage(
                        usage,
                        cost_usd=cost,
                        usage_id="crewai:single:model:1",
                    )
                ctx.extra["usage"] = usage
            guard.check()
        except PromptAdmissionError as exc:
            self._record_budget(ctx, guard, str(exc))
            yield ev_error(code=exc.code, message=str(exc))
            return
        except BudgetExceeded as exc:
            self._record_budget(ctx, guard, exc.reason)
            yield ev_error(
                code="agent_budget_exceeded",
                message=f"Agent execution budget exceeded: {exc.reason}",
                usage=ctx.extra.get("usage"),
                finish_reason="budget",
                budget=ctx.extra.get("budget"),
            )
            yield ev_done(
                message_id=ctx.assistant_msg.id,
                finish_reason="budget",
                usage=ctx.extra.get("usage"),
                budget=ctx.extra.get("budget"),
            )
            return
        except Exception as exc:  # noqa: BLE001
            failure_usage = aggregate_usage(_extract_usage_rounds(exc))
            if failure_usage:
                # Providers without cumulative summary APIs may still attach
                # usage to their terminal exception. Charge it only when no
                # outer delta or real-time wrapper already owns the attempt.
                if metered_usage is None and not realtime_usage_charged():
                    cost = failure_usage.get("cost_usd")
                    if cost is None:
                        cost = usage_cost(
                            getattr(
                                getattr(ctx, "model_config", None),
                                "model_name",
                                None,
                            ),
                            failure_usage,
                        )
                    guard.add_usage(
                        failure_usage,
                        cost_usd=cost,
                        usage_id="crewai:single:failure",
                    )
                ctx.extra["usage"] = failure_usage
            logger.exception("crewai kickoff failed: %s", exc)
            yield ev_error(
                code="crewai_run_error",
                message=str(exc),
                usage=ctx.extra.get("usage"),
            )
            return

        final_text = str(getattr(result, "raw", "") or "")
        ctx.assistant_msg.content = final_text
        if final_text:
            yield ev_token(delta=final_text)
        ctx.extra["finish_reason"] = "stop"
        snapshot = guard.snapshot()
        ctx.extra["budget"] = snapshot
        assistant = ctx.assistant_msg
        assistant.metadata_ = {
            **(getattr(assistant, "metadata_", None) or {}), "budget": snapshot
        }
        yield ev_done(
            message_id=ctx.assistant_msg.id,
            finish_reason="stop",
            usage=ctx.extra.get("usage"),
            budget=snapshot,
        )

    # ====================================================================== #
    # Multi-agent path — real lifecycle orchestration
    # ====================================================================== #
    async def _run_multi_agent(
        self, ctx: AgentTurnContext, llm: Any, profile: str
    ) -> AsyncIterator[AgentEvent]:
        guard = _guard_for_context(ctx)
        stage_ctx = make_stage_context(ctx.run_id, budget_guard=guard)
        # Populate the streaming-writer fields so the writer stage can call the
        # provider directly and mutate the assistant message token-by-token.
        # All Optional; harmless for the non-writer stages and for fakes/demos.
        try:
            stage_ctx.provider = get_provider_for_config(ctx.model_config)
        except Exception as exc:  # noqa: BLE001 — provider build must not kill the run
            logger.warning("could not build provider for streaming writer: %s", exc)
            stage_ctx.provider = None
        stage_ctx.model_config = ctx.model_config
        stage_ctx.assistant_msg = ctx.assistant_msg
        stage_ctx.user_content = ctx.user_content
        stage_ctx.cancel_event = asyncio.Event()
        stage_ctx.db = ctx.db
        stage_ctx.persistence_session_factory = ctx.extra.get(
            "persistence_session_factory"
        ) or AsyncSessionLocal
        stage_ctx.persistence_lock = ctx.extra.get("persistence_lock")
        persist_checkpoint = ctx.extra.get("persist_continuation_checkpoint")
        if callable(persist_checkpoint):
            stage_ctx.persist_continuation_checkpoint = persist_checkpoint
        else:
            async def persist_checkpoint_fallback(checkpoint: dict[str, Any]) -> None:
                from app.services.chat_service import (
                    _persist_continuation_checkpoint,
                )

                async with db_mutation_scope(stage_ctx.persistence_lock):
                    await _persist_continuation_checkpoint(
                        stage_ctx.persistence_session_factory,
                        ctx.assistant_msg,
                        ctx.run_id,
                        checkpoint,
                    )

            stage_ctx.persist_continuation_checkpoint = persist_checkpoint_fallback
        tools = await self._build_tools(ctx, stage_ctx=stage_ctx)

        # The approval bridge is built after the emitter (it needs the emitter
        # to emit waiting/run_status). We attach it to the stage ctx below.
        approval_bridge_holder: dict[str, Any] = {}

        # Build graph + stages for the profile.
        try:
            if profile == "parallel_research":
                graph, stages = build_parallel_research_stages(
                    llm=llm, tools=tools, question=ctx.user_content
                )
            elif profile == "debate":
                graph, stages = build_debate_stages(
                    llm=llm, tools=tools, question=ctx.user_content
                )
            else:
                graph, stages = build_research_stages(
                    llm=llm, tools=tools, question=ctx.user_content
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("crewai multi-agent setup failed: %s", exc)
            yield ev_error(code="crewai_setup_error", message=str(exc))
            return

        executor: StageExecutor = ctx.extra.get("stage_executor")
        if executor is None:
            # Demo isolation: the deterministic DemoStageExecutor (canned,
            # non-real answers) runs ONLY on the strict double opt-in resolved by
            # _demo_executor_enabled (env flag + per-request flag, and real
            # CrewAI NOT enabled). A normal /api/chat/stream turn has demo=False,
            # so it ALWAYS runs the real executor below — the canned "CrewAI
            # supports stateful Flows…" text can never reach a real user this
            # way. (Tests inject their own executor via ctx.extra["stage_executor"].)
            if _demo_executor_enabled(get_settings(), ctx.request):
                executor = DemoStageExecutor()
                ctx.extra["is_demo"] = True
            else:
                # Real path: wrap the CrewAI executor so the writer stage
                # streams its answer token-by-token (see StreamingWriterExecutor)
                # while every other stage keeps using aexecute_task unchanged.
                executor = StreamingWriterExecutor(CrewAIStageExecutor())
        emitter = AgentLifecycleEmitter(run_id=ctx.run_id, graph=graph, stage_ctx=stage_ctx)

        # Wire the cross-thread approval bridge so dangerous tools pause the
        # agent node + run and resume on user approval.
        approval_bridge = ApprovalBridge(
            loop=stage_ctx.loop, stage_ctx=stage_ctx, emitter=emitter, run_id=ctx.run_id,
        )
        stage_ctx.approval_bridge = approval_bridge

        # Persist the static graph_definition once.
        await self._persist_graph(ctx, emitter, definition=True)

        # ---- Phase 2: publish a draft research plan (deep_research) ----
        # Built deterministically from the question; the UI shows it in the
        # Context Panel and the user may confirm/adjust. When
        # PLAN_REQUIRE_CONFIRMATION is enabled the run GATES here (bounded by
        # PLAN_CONFIRM_TIMEOUT_S) until the user confirms/revises — making the
        # plan/confirm endpoints a true approval gate. When disabled (default)
        # the plan is advisory: published for review while the run proceeds.
        try:
            from app.core.config import get_settings

            _plan_gate = bool(getattr(get_settings(), "PLAN_REQUIRE_CONFIRMATION", False))
            intent_lbl = ctx.extra.get("intent") or "chat"
            plan_summary, plan_steps = build_plan(intent_lbl, ctx.user_content)
            plan = {
                "summary": plan_summary,
                "steps": [
                    {"id": s["id"], "title": s["title"], "sources": ["knowledge_base", "web"]}
                    for s in plan_steps
                ],
                "acceptanceCriteria": _acceptance_criteria_for(ctx.user_content, plan_steps),
                "requires_confirmation": _plan_gate,
            }
            async with db_mutation_scope(stage_ctx.persistence_lock):
                await persist_research_plan(
                    stage_ctx.persistence_session_factory,
                    run_id=ctx.run_id,
                    plan=plan,
                )
            stage_ctx.emit(ev_research_plan(
                run_id=ctx.run_id,
                status="draft",
                summary=plan_summary,
                steps=plan["steps"],
                requires_confirmation=_plan_gate,
            ))
            if _plan_gate:
                gated = await self._await_plan_confirmation(ctx, stage_ctx)
                if not gated:
                    # Timed out waiting: proceed anyway (bounded wait beats a
                    # stuck run) but mark the plan so the audit trail knows.
                    logger.warning(
                        "plan confirmation timed out for run %s; proceeding", ctx.run_id
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — plan is best-effort
            logger.warning("research plan emission failed", exc_info=True)

        # ---- concurrent run + drain ----
        outputs: dict[str, StageResult] = {}
        run_exc: Exception | None = None
        flow_cancelled = False

        async def run_flow() -> None:
            nonlocal run_exc
            try:
                emitter.emit_graph_initialized()
                emitter.emit_run_status("running")
                await self._walk_stages(ctx, stages, emitter, executor, stage_ctx, outputs)
                emitter.emit_run_status("completed")
            except asyncio.CancelledError:
                emitter.emit_run_status("cancelled")
                raise
            except Exception as exc:  # noqa: BLE001
                run_exc = exc
                logger.exception("multi-agent flow failed: %s", exc)
                emitter.emit_run_status("failed")
            finally:
                stage_ctx.close()

        run_task = asyncio.create_task(run_flow())
        try:
            while True:
                evt = await stage_ctx.queue.get()
                if evt is None:
                    break
                # Persist live graph_state on structural events (cheap; few).
                if evt.kind in ("agent_graph", "agent_status", "agent_edge", "run_status"):
                    await self._persist_graph(ctx, emitter, definition=False)
                # Durable control-state events (user pause/resume/instruction):
                # persist to run_events so the cursor-replay SSE
                # (/api/agent-runs/{id}/events) — which RunControls subscribes
                # to — sees the paused state and can show the 恢复 button.
                # Without this the execution truly pauses but the /events
                # stream never reports it (the chat SSE is a separate feed).
                if evt.kind in ("run_paused", "run_resumed", "run_instruction_received"):
                    await append_event_safe(
                        ctx.db, ctx.run_id, evt.kind, dict(evt.data or {})
                    )
                yield evt
        finally:
            # If the stream is cancelled/closed while a tool is paused on
            # approval, unblock the worker thread so it doesn't leak.
            approval_bridge.cancel_active()
            if not run_task.done():
                run_task.cancel()
            try:
                # Await even an already-done task: otherwise a completed
                # CancelledError is never observed and the runtime falls
                # through to a fabricated done/stop event.
                await run_task
            except asyncio.CancelledError:
                flow_cancelled = True
            except Exception as exc:  # pragma: no cover - run_flow captures these
                if run_exc is None:
                    run_exc = exc
            # Final snapshot persist.
            await self._persist_graph(ctx, emitter, definition=False)
            partial_usage = _aggregate_crewai_usage(stages, outputs, stage_ctx)
            if partial_usage is not None:
                ctx.extra["usage"] = partial_usage

        if flow_cancelled:
            raise asyncio.CancelledError()

        if run_exc is not None:
            # Preserve any partial content the writer streamed before failing
            # (the old code wiped assistant_msg.content here, losing partials).
            finish, code = _map_run_error(run_exc)
            ctx.extra["finish_reason"] = finish
            hard_budget_exceeded = isinstance(run_exc, BudgetExceeded)
            if hard_budget_exceeded:
                self._record_budget(ctx, guard, run_exc.reason)
            yield ev_error(
                code=code,
                message=str(run_exc),
                usage=ctx.extra.get("usage"),
                finish_reason="budget" if hard_budget_exceeded else None,
                budget=(ctx.extra.get("budget") if hard_budget_exceeded else None),
            )
            if hard_budget_exceeded:
                yield ev_done(
                    message_id=ctx.assistant_msg.id,
                    finish_reason="budget",
                    usage=ctx.extra.get("usage"),
                    budget=ctx.extra.get("budget"),
                )
            return

        # The final stage (writer) holds the answer — unless it already streamed
        # its tokens via StreamingWriterExecutor (in which case the assistant
        # message content was set incrementally and we must not re-emit).
        streamed = bool(getattr(stage_ctx, "writer_streamed", False))
        # If the writer "streamed" but produced no content (a transient model
        # blank even after the writer's own retry), do NOT leave a blank bubble:
        # fall through to the upstream-output fallback so the user still sees the
        # research (analyst findings) instead of nothing.
        if streamed and not (ctx.assistant_msg.content or "").strip():
            streamed = False
        final_text = ""
        if not streamed:
            for spec in reversed(stages):
                res = outputs.get(spec.agent_id)
                if res and res.raw:
                    final_text = res.raw
                    break
            ctx.assistant_msg.content = final_text
        # Recover the writer's REAL finish_reason (length/stop/cancelled/...)
        # instead of clobbering it with a hard-coded "stop".
        finish = _writer_finish_reason(stages, outputs)
        usage = _aggregate_crewai_usage(stages, outputs, stage_ctx)
        if usage is not None:
            ctx.extra["usage"] = usage
        ctx.extra["finish_reason"] = finish
        ctx.extra["multi_agent"] = True
        snapshot = guard.snapshot()
        ctx.extra["budget"] = snapshot
        assistant = ctx.assistant_msg
        assistant.metadata_ = {
            **(getattr(assistant, "metadata_", None) or {}), "budget": snapshot
        }
        if not streamed and final_text:
            yield ev_token(delta=final_text)
        yield ev_done(
            message_id=ctx.assistant_msg.id,
            finish_reason=finish,
            usage=usage,
            budget=snapshot,
        )

    async def _await_plan_confirmation(self, ctx, stage_ctx) -> bool:
        """Block until the user confirms/revises the plan, or the timeout hits.

        Polls the durable run row (an isolated short session per poll — the
        request-side plan/confirm endpoint writes plan_status there). Honors
        cancel via the run control. Returns True when confirmed/updated, False
        on timeout.
        """
        from app.core.config import get_settings

        timeout_s = int(getattr(get_settings(), "PLAN_CONFIRM_TIMEOUT_S", 300))
        ctl = ctx.extra.get("run_control") or get_run_control(ctx.run_id)
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            if ctl is not None and ctl.cancel.is_set():
                raise asyncio.CancelledError()
            try:
                async with db_mutation_scope(stage_ctx.persistence_lock):
                    factory = stage_ctx.persistence_session_factory
                    async with factory() as session:
                        row = await session.execute(
                            select(AgentRun.plan_status).where(AgentRun.id == ctx.run_id)
                        )
                        status = row.scalar_one_or_none()
                if status in ("confirmed", "updated"):
                    return True
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — poll is best-effort
                logger.debug("plan-status poll failed", exc_info=True)
            await asyncio.sleep(1.0)
        return False

    async def _respect_controls(self, ctx, stage_ctx, emitter) -> None:
        """Honor user pause/resume + drain appended instructions between stages.

        Also consumes DURABLE commands (``run_commands`` rows) here (B8): the
        API persists pause/resume/cancel/instruction as RunCommands before the
        in-process signal; claiming+applying them here makes those rows real
        (exactly-once) and covers the case where the live signal missed (e.g.
        a different worker process held the run).
        """
        ctl = ctx.extra.get("run_control") or get_run_control(ctx.run_id)
        if ctl is None:
            return
        guard = stage_ctx.budget_guard
        if guard is not None:
            guard.check()
        # Honor a user-initiated cancel between stages.
        if ctl.cancel.is_set():
            raise asyncio.CancelledError()
        # Durable command drain (exactly-once claim → apply → mark).
        await self._drain_durable_commands(ctx, stage_ctx, ctl)
        pending = ctl.drain_instructions()
        for instr in pending:
            stage_ctx.emit(ev_run_instruction_received(run_id=ctx.run_id, instruction=instr))
            stage_ctx.pending_instructions.append(instr)
        if ctl.is_paused():
            stage_ctx.emit(ev_run_paused(run_id=ctx.run_id, reason="user"))
            while ctl.is_paused():
                if ctl.cancel.is_set():
                    break
                if guard is None:
                    await asyncio.sleep(0.1)
                else:
                    try:
                        async with asyncio.timeout(guard.remaining_seconds):
                            await asyncio.sleep(0.1)
                    except TimeoutError as exc:
                        raise BudgetExceeded(
                            f"time budget ({guard.limits.max_runtime_seconds}s) exceeded"
                        ) from exc
                    guard.check()
            stage_ctx.emit(ev_run_resumed(run_id=ctx.run_id))

    async def _drain_durable_commands(self, ctx, stage_ctx, ctl) -> None:
        """Claim + apply pending durable RunCommands for this run (B8).

        Maps command types onto the in-process RunControl (pause/resume/cancel/
        instruction). Each command is marked applied/failed exactly once. Best-
        effort: a store failure never breaks the stage walk.
        """
        try:
            from app.agents.workflow.repository import CommandStore

            async with db_mutation_scope(stage_ctx.persistence_lock):
                factory = stage_ctx.persistence_session_factory
                async with factory() as session:
                    store = CommandStore(session)
                    commands = await store.claim_pending(ctx.run_id)
                    for cmd in commands or []:
                        ctype = cmd.command_type
                        payload = dict(cmd.payload or {})
                        try:
                            if ctype == "pause":
                                ctl.pause()
                            elif ctype == "resume":
                                ctl.resume()
                            elif ctype == "cancel":
                                ctl.cancel.set()
                            elif ctype == "instruction":
                                text = str(payload.get("text") or "").strip()
                                if text:
                                    ctl.add_instruction(text)
                            elif ctype in ("approve", "reject"):
                                # Consumed by the approval bus coordinator, not
                                # here. Revert the claim so its dedicated
                                # consumer still finds the row pending.
                                cmd.status = "pending"
                                cmd.claimed_at = None
                                cmd.claimed_by = None
                                await session.flush()
                                continue
                            await store.mark_applied(cmd.id)
                        except Exception as exc:  # noqa: BLE001
                            await store.mark_failed(cmd.id, str(exc)[:500])
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — durable drain is best-effort
            logger.debug("durable command drain failed", exc_info=True)

    async def _walk_stages(
        self,
        ctx: AgentTurnContext,
        stages: list[StageSpec],
        emitter: AgentLifecycleEmitter,
        executor: StageExecutor,
        stage_ctx: StageContext,
        outputs: dict[str, StageResult],
    ) -> None:
        """Execute stages grouped by ``stage`` number; same-stage specs run in
        parallel via ``asyncio.gather``. Joins are enforced by the emitter."""
        # Group by stage, preserving stage order.
        stage_groups: dict[int, list[StageSpec]] = {}
        for spec in stages:
            stage_groups.setdefault(spec.stage, []).append(spec)

        for stage_num in sorted(stage_groups):
            group = stage_groups[stage_num]
            await self._respect_controls(ctx, stage_ctx, emitter)
            if len(group) == 1:
                await self._run_one_stage(group[0], emitter, executor, stage_ctx, outputs)
            else:
                # Parallel: run all specs in this stage concurrently. Each emits
                # its own agent_started/completed; multiple are running at once.
                # Fail-fast: gather raises on the first failure; we cancel the
                # downstream. Siblings still in flight are awaited (returned_ex).
                tasks = [
                    asyncio.create_task(
                        self._run_one_stage(s, emitter, executor, stage_ctx, outputs)
                    )
                    for s in group
                ]
                try:
                    await asyncio.gather(*tasks)
                except Exception:
                    # Cancel any not-yet-finished sibling so we don't leak tasks.
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise

    async def _run_one_stage(
        self,
        spec: StageSpec,
        emitter: AgentLifecycleEmitter,
        executor: StageExecutor,
        stage_ctx: StageContext,
        outputs: dict[str, StageResult],
    ) -> None:
        """Run a single agent stage with real lifecycle events around it."""
        guard = stage_ctx.budget_guard
        # Build the context string from dependency outputs (the handoff).
        context_parts = []
        for dep_id in spec.depends_on:
            dep = outputs.get(dep_id)
            if dep and dep.raw:
                context_parts.append(f"[{dep_id} output]\n{dep.raw}")
        # Phase 2: inject any user instructions appended since the last stage.
        if stage_ctx.pending_instructions:
            context_parts.append(
                "[用户追加指导]\n" + "\n".join(f"- {i}" for i in stage_ctx.pending_instructions)
            )
            stage_ctx.pending_instructions = []
        context_str = "\n\n".join(context_parts) if context_parts else None

        started = emitter.emit_agent_started(spec.agent_id, task_title=spec.task.description[:80] if hasattr(spec.task, "description") else None)
        if not started:
            # Waiting on a join — the emitter already moved it to waiting. Skip
            # execution until predecessors complete (handled by stage ordering,
            # so this branch is a safety net for malformed graphs).
            return

        try:
            if guard is None:
                result = await executor.execute(
                    agent_id=spec.agent_id,
                    agent=spec.agent,
                    task=spec.task,
                    context=context_str,
                    stage_ctx=stage_ctx,
                )
            else:
                guard.check()
                try:
                    async with asyncio.timeout(guard.remaining_seconds):
                        result = await executor.execute(
                            agent_id=spec.agent_id,
                            agent=spec.agent,
                            task=spec.task,
                            context=context_str,
                            stage_ctx=stage_ctx,
                        )
                except TimeoutError as exc:
                    raise BudgetExceeded(
                        f"time budget ({guard.limits.max_runtime_seconds}s) exceeded"
                    ) from exc
            outputs[spec.agent_id] = result
            if guard is not None and result.usage and not result.usage_charged:
                cost = result.usage.get("cost_usd")
                if cost is None:
                    cost = usage_cost(
                        getattr(stage_ctx.model_config, "model_name", None),
                        result.usage,
                    )
                guard.add_usage(
                    result.usage,
                    cost_usd=cost,
                    usage_id=f"crewai:stage:{spec.agent_id}",
                )
                guard.check()
            emitter.emit_agent_completed(
                spec.agent_id, output_summary=result.output_summary or None
            )
        except asyncio.CancelledError:
            emitter.emit_agent_cancelled(spec.agent_id)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("stage %s failed: %s", spec.agent_id, exc)
            emitter.emit_agent_failed(spec.agent_id, error=str(exc))
            # Fail-fast: cancel everything downstream of this node.
            emitter.cancel_downstream(spec.agent_id)
            raise

    # ====================================================================== #
    # Helpers
    # ====================================================================== #
    async def _build_tools(
        self, ctx: AgentTurnContext, *, stage_ctx: StageContext | None
    ) -> list[Any]:
        if not ctx.enable_tools:
            return []
        from app.agents.intent_router import filter_tool_names

        registry = __import__("app.tools.registry_init", fromlist=["get_default_registry"]).get_default_registry()
        # Merge statically-configured MCP server tools so the CrewAI runtime also
        # offers them; each wrapper routes through ToolGateway (via the shared
        # budget/audit path) like a builtin. No-op when MCP is unconfigured.
        from app.agents.mcp_client import merge_mcp_tools

        merge_mcp_tools(registry)
        user_id = ctx.user.id if ctx.user else None
        # Per-tenant connector→session lifecycle (Task 9 follow-up): merge THIS
        # user's enabled-connector tools through the same gateway path. The
        # manager is cached on ctx.extra so multi-stage runs open sessions once
        # and reuse them across stages (idempotent open_for_user); stream_turn's
        # finally closes them at run end.
        if user_id is not None:
            _stashed_mgr = ctx.extra.get("_connector_session_manager")
            _local_mgr = None
            try:
                if _stashed_mgr is None:
                    from app.connectors.sessions import ConnectorSessionManager

                    _local_mgr = ConnectorSessionManager(ctx.db)
                    _conn_registry = await _local_mgr.open_for_user(user_id)
                    merge_mcp_tools(registry, mcp_registry=_conn_registry)
                    ctx.extra["_connector_session_manager"] = _local_mgr
                else:
                    _conn_registry = await _stashed_mgr.open_for_user(user_id)
                    merge_mcp_tools(registry, mcp_registry=_conn_registry)
            except Exception:  # noqa: BLE001 — connector tools must never crash the run
                logger.warning(
                    "crewai connector→session merge failed for run %s; skipping",
                    ctx.run_id, exc_info=True,
                )
                # Defensive: a freshly-created (uncached) manager that failed
                # before being stashed must be closed so sessions don't leak.
                if _local_mgr is not None and _stashed_mgr is None:
                    try:
                        await _local_mgr.close_all()
                    except Exception:  # noqa: BLE001
                        pass
        # Apply the intent route's allowlist / disable_web (search / create modes).
        route = ctx.extra.get("route")
        sources = list(registry.list())
        if route is not None:
            allowed = set(filter_tool_names([s.name for s in sources], route))
            sources = [s for s in sources if s.name in allowed]
        tools: list[Any] = []
        for src in sources:
            try:
                tools.append(
                    build_crewai_tool(
                        src,
                        conversation_id=ctx.conversation.id,
                        message_id=ctx.assistant_msg.id,
                        run_id=ctx.run_id,
                        user_id=user_id,
                        stage_ctx=stage_ctx,
                        budget_guard=ctx.budget_guard,
                        max_result_chars=(
                            ctx.budget_guard.limits.max_tool_output_chars
                            if ctx.budget_guard is not None
                            else 8_000
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to adapt tool %s: %s", src.name, exc)
        return tools

    @staticmethod
    def _record_budget(
        ctx: AgentTurnContext, guard: BudgetGuard, reason: str
    ) -> None:
        ctx.extra["finish_reason"] = "budget"
        ctx.extra["budget_exceeded_reason"] = reason
        snapshot = guard.snapshot()
        if snapshot.get("reason") is None:
            snapshot["exhausted"] = True
            snapshot["reason"] = reason
        ctx.extra["budget"] = snapshot
        assistant = ctx.assistant_msg
        assistant.metadata_ = {
            **(getattr(assistant, "metadata_", None) or {}), "budget": snapshot
        }

    async def _persist_graph(
        self,
        ctx: AgentTurnContext,
        emitter: AgentLifecycleEmitter,
        *,
        definition: bool,
    ) -> None:
        """Write the graph snapshot to the AgentRun row (best-effort)."""
        session_factory = ctx.extra.get("persistence_session_factory") or AsyncSessionLocal
        try:
            async with db_mutation_scope(ctx.extra.get("persistence_lock")):
                await persist_graph_snapshot(
                    session_factory,
                    run_id=ctx.run_id,
                    snapshot=emitter.snapshot(),
                    definition=definition,
                )
        except BaseException as exc:
            if isinstance(exc, Exception):
                logger.warning("failed to persist agent graph snapshot", exc_info=True)
                return
            raise
