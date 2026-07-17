"""CrewAI runtime: runs a turn as a single-agent Crew.

Phase 1 scope — minimal tech validation:
  * LLM built from the existing :class:`ModelConfig` (no duplicated config).
  * Tools wrapped via :class:`~app.agents.adapters.tool_adapter` so every call
    goes through :class:`~app.agents.gateway.tool_gateway.ToolGateway`
    (permission / approval / audit / truncation) — the same path as native.
  * The tool trace is emitted from the authoritative ``agent_steps`` rows the
    gateway wrote, so the frontend's step panel works regardless of CrewAI's
    internal event shapes.
  * The final answer (``CrewOutput.raw``) is emitted as token(s).

Live LLM token streaming and intent-routed Flows arrive in Phase 2 (CrewAI's
event bus / Flows streaming need a live model to validate, so Phase 1 stays
deterministic and robust). ``crewai`` is imported lazily so the app boots
without it.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from sqlalchemy import select

from app.agents.adapters.llm_adapter import CrewAILLMFactory
from app.agents.adapters.tool_adapter import build_crewai_tool
from app.agents.crews import build_research_crew, review_crew_output
from app.agents.planning import build_plan, classify_intent
from app.agents.schemas import (
    AgentEvent,
    AgentTurnContext,
    ev_done,
    ev_error,
    ev_plan_created,
    ev_step_completed,
    ev_step_started,
    ev_token,
    ev_tool_call,
    ev_tool_result,
)
from app.models import AgentStep
from app.tools.registry_init import get_default_registry

logger = logging.getLogger(__name__)


class CrewAIRuntime:
    """Single-agent CrewAI runtime. Implements the :class:`AgentRuntime` protocol."""

    name = "crewai"

    async def stream_turn(self, ctx: AgentTurnContext) -> AsyncIterator[AgentEvent]:
        # 1. LLM from the existing ModelConfig.
        try:
            llm = CrewAILLMFactory.from_model_config(ctx.model_config)
        except Exception as exc:  # noqa: BLE001
            logger.exception("crewai LLM build failed: %s", exc)
            yield ev_error(code="crewai_llm_error", message=str(exc))
            return

        # 2. Tool adapters (all go through the gateway).
        tools: list[Any] = []
        if ctx.enable_tools:
            registry = get_default_registry()
            user_id = ctx.user.id if ctx.user else None
            for src in registry.list():
                try:
                    tools.append(
                        build_crewai_tool(
                            src,
                            conversation_id=ctx.conversation.id,
                            message_id=ctx.assistant_msg.id,
                            run_id=ctx.run_id,
                            user_id=user_id,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("failed to adapt tool %s: %s", src.name, exc)

        # 3. Route by intent: deep_research -> Researcher/Analyst/Writer crew;
        #    anything else -> a single agent. Plain chat should never reach here
        #    (the orchestrator only selects CrewAI for execution_mode=agent).
        intent = classify_intent(ctx.user_content) if ctx.enable_tools else "chat"
        ctx.extra["intent"] = intent
        plan_summary, plan_steps = build_plan(intent, ctx.user_content)
        yield ev_plan_created(summary=plan_summary, steps=plan_steps)

        try:
            if intent == "deep_research":
                crew = build_research_crew(
                    llm=llm,
                    tools=tools,
                    question=ctx.user_content,
                    run_id=ctx.run_id,
                )
                # Announce the three roles as steps for the execution trace.
                for sid, title in (("1", "Researcher 检索证据"), ("2", "Analyst 核对证据"), ("3", "Writer 生成答案")):
                    yield ev_step_started(step_id=sid, title=title, step_type="agent", agent=title.split(" ")[0])
            else:
                from crewai import Agent, Crew, Process, Task

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

        # 4. Run the crew to completion.
        try:
            result = await crew.kickoff_async()
        except Exception as exc:  # noqa: BLE001
            logger.exception("crewai kickoff failed: %s", exc)
            yield ev_error(code="crewai_run_error", message=str(exc))
            return

        # Close out the announced step(s).
        step_ids = ("1", "2", "3") if intent == "deep_research" else ()
        for sid in step_ids:
            yield ev_step_completed(step_id=sid, status="done")

        # 5. Emit the tool trace from the authoritative agent_steps rows.
        async for evt in self._emit_tool_trace(ctx):
            yield evt

        # 7. Final answer.
        final_text = ""
        try:
            final_text = str(getattr(result, "raw", "") or "")
        except Exception:  # noqa: BLE001
            final_text = ""
        ctx.assistant_msg.content = final_text
        if final_text:
            yield ev_token(delta=final_text)

        ctx.extra["finish_reason"] = "stop"
        yield ev_done(message_id=ctx.assistant_msg.id, finish_reason="stop")

    async def _emit_tool_trace(
        self, ctx: AgentTurnContext
    ) -> AsyncIterator[AgentEvent]:
        """Replay tool calls as tool_call/tool_result events from agent_steps.

        The gateway (invoked by the tool adapter) writes these rows on its own
        session, so this read sees them after ``kickoff_async`` returns.
        """
        registry = get_default_registry()
        rows = (
            await ctx.db.execute(
                select(AgentStep)
                .where(AgentStep.run_id == ctx.run_id)
                .order_by(AgentStep.sequence)
            )
        ).scalars().all()

        for s in rows:
            if s.step_type != "tool":
                continue
            args = s.input_redacted or {}
            dangerous = _is_dangerous(registry, s.tool_name)
            yield ev_tool_call(
                id=str(s.id), name=s.tool_name, arguments=args, dangerous=dangerous
            )
            ok = s.status == "done"
            yield ev_tool_result(
                id=str(s.id),
                name=s.tool_name,
                ok=ok,
                result=s.output_redacted,
                error=None if ok else "tool error",
            )


def _is_dangerous(registry, name: str) -> bool:
    try:
        return bool(getattr(registry.get(name), "dangerous", False))
    except Exception:  # noqa: BLE001
        return False
