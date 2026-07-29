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
import logging
import uuid
from typing import Any, AsyncIterator

from app.agents.approval_bridge import ApprovalBridge
from app.agents.adapters.llm_adapter import CrewAILLMFactory
from app.agents.adapters.tool_adapter import build_crewai_tool
from app.agents.crews import (
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
)
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
from app.core.config import get_settings
from app.models import AgentRun
from app.providers.registry import get_provider_for_config

logger = logging.getLogger(__name__)

# Profiles that use the multi-agent graph + right-side panel.
_MULTI_AGENT_PROFILES = {"deep_research", "parallel_research"}


class CrewAIRuntime:
    """Multi-agent CrewAI runtime. Implements the :class:`AgentRuntime` protocol."""

    name = "crewai"

    async def stream_turn(self, ctx: AgentTurnContext) -> AsyncIterator[AgentEvent]:
        # 1. LLM from the existing ModelConfig.
        try:
            llm = CrewAILLMFactory.from_model_config(ctx.model_config)
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

        tools = self._build_tools(ctx, stage_ctx=None)
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

        try:
            result = await crew.kickoff_async()
        except Exception as exc:  # noqa: BLE001
            logger.exception("crewai kickoff failed: %s", exc)
            yield ev_error(code="crewai_run_error", message=str(exc))
            return

        final_text = str(getattr(result, "raw", "") or "")
        ctx.assistant_msg.content = final_text
        if final_text:
            yield ev_token(delta=final_text)
        ctx.extra["finish_reason"] = "stop"
        yield ev_done(message_id=ctx.assistant_msg.id, finish_reason="stop")

    # ====================================================================== #
    # Multi-agent path — real lifecycle orchestration
    # ====================================================================== #
    async def _run_multi_agent(
        self, ctx: AgentTurnContext, llm: Any, profile: str
    ) -> AsyncIterator[AgentEvent]:
        stage_ctx = make_stage_context(ctx.run_id)
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
        tools = self._build_tools(ctx, stage_ctx=stage_ctx)

        # The approval bridge is built after the emitter (it needs the emitter
        # to emit waiting/run_status). We attach it to the stage ctx below.
        approval_bridge_holder: dict[str, Any] = {}

        # Build graph + stages for the profile.
        try:
            if profile == "parallel_research":
                graph, stages = build_parallel_research_stages(
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
            # Demo mode lets the full panel run live without an external LLM.
            if getattr(get_settings(), "AGENT_DEMO_MODE", False):
                executor = DemoStageExecutor()
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
        # Context Panel and the user may confirm/adjust. requires_confirmation
        # is False so the run proceeds (the plan/confirm endpoint still records
        # the decision on the run row).
        try:
            intent_lbl = ctx.extra.get("intent") or "chat"
            plan_summary, plan_steps = build_plan(intent_lbl, ctx.user_content)
            plan = {
                "summary": plan_summary,
                "steps": [
                    {"id": s["id"], "title": s["title"], "sources": ["knowledge_base", "web"]}
                    for s in plan_steps
                ],
                "requires_confirmation": False,
            }
            run_row = await ctx.db.get(AgentRun, ctx.run_id)
            if run_row is not None:
                run_row.plan = plan
                run_row.plan_status = "draft"
                await ctx.db.commit()
            stage_ctx.emit(ev_research_plan(
                run_id=ctx.run_id,
                status="draft",
                summary=plan_summary,
                steps=plan["steps"],
                requires_confirmation=False,
            ))
        except Exception:  # noqa: BLE001 — plan is best-effort
            logger.warning("research plan emission failed", exc_info=True)

        # ---- concurrent run + drain ----
        outputs: dict[str, StageResult] = {}
        run_error: str | None = None

        async def run_flow() -> None:
            nonlocal run_error
            try:
                emitter.emit_graph_initialized()
                emitter.emit_run_status("running")
                await self._walk_stages(ctx, stages, emitter, executor, stage_ctx, outputs)
                emitter.emit_run_status("completed")
            except asyncio.CancelledError:
                emitter.emit_run_status("cancelled")
                raise
            except Exception as exc:  # noqa: BLE001
                run_error = str(exc)
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
                yield evt
        finally:
            # If the stream is cancelled/closed while a tool is paused on
            # approval, unblock the worker thread so it doesn't leak.
            approval_bridge.cancel_active()
            if not run_task.done():
                run_task.cancel()
                try:
                    await run_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            # Final snapshot persist.
            await self._persist_graph(ctx, emitter, definition=False)

        if run_error is not None:
            ctx.assistant_msg.content = ""
            ctx.extra["finish_reason"] = "error"
            yield ev_error(code="crewai_run_error", message=run_error)
            return

        # The final stage (writer) holds the answer — unless it already streamed
        # its tokens via StreamingWriterExecutor (in which case the assistant
        # message content was set incrementally and we must not re-emit).
        streamed = bool(getattr(stage_ctx, "writer_streamed", False))
        final_text = ""
        if not streamed:
            for spec in reversed(stages):
                res = outputs.get(spec.agent_id)
                if res and res.raw:
                    final_text = res.raw
                    break
            ctx.assistant_msg.content = final_text
        ctx.extra["finish_reason"] = "stop"
        ctx.extra["multi_agent"] = True
        if not streamed and final_text:
            yield ev_token(delta=final_text)
        yield ev_done(message_id=ctx.assistant_msg.id, finish_reason="stop")

    async def _respect_controls(self, ctx, stage_ctx, emitter) -> None:
        """Honor user pause/resume + drain appended instructions between stages."""
        ctl = ctx.extra.get("run_control") or get_run_control(ctx.run_id)
        if ctl is None:
            return
        # Honor a user-initiated cancel between stages.
        if ctl.cancel.is_set():
            raise asyncio.CancelledError()
        pending = ctl.drain_instructions()
        for instr in pending:
            stage_ctx.emit(ev_run_instruction_received(run_id=ctx.run_id, instruction=instr))
            stage_ctx.pending_instructions.append(instr)
        if ctl.is_paused():
            stage_ctx.emit(ev_run_paused(run_id=ctx.run_id, reason="user"))
            while ctl.is_paused():
                if ctl.cancel.is_set():
                    break
                await asyncio.sleep(0.1)
            stage_ctx.emit(ev_run_resumed(run_id=ctx.run_id))

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
            result = await executor.execute(
                agent_id=spec.agent_id,
                agent=spec.agent,
                task=spec.task,
                context=context_str,
                stage_ctx=stage_ctx,
            )
            outputs[spec.agent_id] = result
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
    def _build_tools(
        self, ctx: AgentTurnContext, *, stage_ctx: StageContext | None
    ) -> list[Any]:
        if not ctx.enable_tools:
            return []
        from app.agents.intent_router import filter_tool_names

        registry = __import__("app.tools.registry_init", fromlist=["get_default_registry"]).get_default_registry()
        user_id = ctx.user.id if ctx.user else None
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
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to adapt tool %s: %s", src.name, exc)
        return tools

    async def _persist_graph(
        self,
        ctx: AgentTurnContext,
        emitter: AgentLifecycleEmitter,
        *,
        definition: bool,
    ) -> None:
        """Write the graph snapshot to the AgentRun row (best-effort)."""
        try:
            run = await ctx.db.get(AgentRun, ctx.run_id)
            if run is None:
                return
            snap = emitter.snapshot()
            if definition:
                run.graph_definition = snap
            run.graph_state = snap
            await ctx.db.commit()
        except Exception:  # pragma: no cover - persistence is best-effort
            logger.warning("failed to persist agent graph snapshot", exc_info=True)
            try:
                await ctx.db.rollback()
            except Exception:
                pass
