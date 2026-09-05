"""Step execution abstraction for the workflow engine (Task 6).

A :class:`StepExecutor` runs a single :class:`~app.agents.workflow.schemas.Step`
given its upstream observations and returns a checkpointed
:class:`~app.agents.workflow.schemas.StepObservation`. The engine drives
ready-set computation, bounded concurrency, and retry AROUND this call — so any
implementation (real CrewAI stage runner, a stub, an LLM call) plugs in here.

This module ships:

  * :class:`StepExecutor` — the :class:`typing.Protocol` the engine consumes.
  * :class:`DefaultStepExecutor` — wraps a plain callable for ad-hoc wiring.
  * :class:`RecordingExecutor` — a deterministic test double (no LLM) that
    records the order steps started and yields to make concurrency observable.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from app.agents.workflow.schemas import Step, StepObservation


@runtime_checkable
class StepExecutor(Protocol):
    """Execute one step. Raise to signal failure (see :class:`StepError`)."""

    async def execute(
        self,
        step: Step,
        upstream: dict[str, StepObservation],
    ) -> StepObservation: ...


# A callable executor: async (step, upstream) -> StepObservation.
ExecuteFn = Callable[
    [Step, dict[str, StepObservation]],
    Awaitable[StepObservation],
]


class DefaultStepExecutor:
    """Adapts a plain async callable to the :class:`StepExecutor` protocol."""

    def __init__(self, fn: ExecuteFn) -> None:
        self._fn = fn

    async def execute(
        self, step: Step, upstream: dict[str, StepObservation]
    ) -> StepObservation:
        return await self._fn(step, upstream)


class RecordingExecutor:
    """Deterministic test double.

    Returns a canned output per step id (default ``[<id>]``) and records the
    order steps started. Critically it ``await asyncio.sleep(0)`` before
    returning so the engine's concurrency is genuinely observable: independent
    ready steps overlap in-flight, which is what ``max_concurrency`` measures.
    """

    def __init__(self, outputs: dict[str, str] | None = None) -> None:
        self.outputs: dict[str, str] = dict(outputs or {})
        self.started_order: list[str] = []

    async def execute(
        self, step: Step, upstream: dict[str, StepObservation]
    ) -> StepObservation:
        self.started_order.append(step.id)
        # Yield so concurrently-scheduled siblings also start before this one
        # finishes (without this a fast stub would serialize accidentally).
        await asyncio.sleep(0)
        out = self.outputs.get(step.id, f"[{step.id}]")
        return StepObservation(step_id=step.id, output=out)


# --------------------------------------------------------------------------- #
# StageAdapterExecutor: thin delegation to the EXISTING CrewAI stage runner.
#
# This is the bridge that lets the engine run a template plan with REAL CrewAI
# agents without reimplementing CrewAI: the agent/task objects come from the
# existing ``build_*_stages`` crew builders (keyed by the same stable agent ids
# the templates emit), and each step delegates to ``CrewAIStageExecutor.execute``
# — the same executor the live ``CrewAIRuntime`` uses. Wiring this into the
# orchestrator's hot path is deferred (see the note in ``orchestrator.py``); the
# class is provided so the integration point is concrete and reviewable.
# --------------------------------------------------------------------------- #
class StageAdapterExecutor:
    """Drive a template plan's steps through the existing CrewAI stage runner.

    ``stages`` maps the plan's step ids (e.g. ``"researcher"``,
    ``"advocate-a"``) to the :class:`~app.agents.crews.stage.StageSpec` objects
    produced by the existing crew builders (``build_research_stages``,
    ``build_parallel_research_stages``, ``build_debate_stages``). ``stage_ctx``
    is the shared :class:`~app.agents.stage_context.StageContext` the runtime
    builds (tool attribution, budget guard, event forwarding).

    On each ``execute``: the upstream observations are serialized into a context
    string, the matching StageSpec is handed to
    :class:`~app.agents.runtime.stage_executor.CrewAIStageExecutor`, and the
    returned :class:`~app.agents.runtime.stage_executor.StageResult` is mapped
    back to a :class:`~app.agents.workflow.schemas.StepObservation`.
    """

    def __init__(self, stages: dict, stage_ctx: Any) -> None:
        # ``stages`` is typed loosely (dict[str, StageSpec]) to avoid importing
        # crewai at module load time; the runtime builds it from the crew
        # builders. Missing ids raise KeyError at execute-time (fail loud).
        self._stages = stages
        self._stage_ctx = stage_ctx
        # Lazy import so the workflow package imports cleanly without crewai.
        from app.agents.runtime.stage_executor import (
            CrewAIStageExecutor,  # noqa: WPS433
        )

        self._inner = CrewAIStageExecutor()

    async def execute(
        self, step: Step, upstream: dict[str, StepObservation]
    ) -> StepObservation:
        spec = self._stages[step.id]
        context = _serialize_upstream(upstream) if upstream else None
        result = await self._inner.execute(
            agent_id=step.id,
            agent=spec.agent,
            task=spec.task,
            context=context,
            stage_ctx=self._stage_ctx,
        )
        return StepObservation(
            step_id=step.id,
            output=result.raw or "",
            structured=result.structured,
            usage=result.usage,
        )


def _serialize_upstream(upstream: dict[str, StepObservation]) -> str:
    """Flatten upstream observations into a context string for the next stage.

    Mirrors what the live runtime feeds into ``aexecute_task`` as ``context``:
    each prior step's output, in dependency order.
    """
    parts: list[str] = []
    for sid, obs in upstream.items():
        body = (obs.output or "").strip()
        if body:
            parts.append(f"[{sid}]\n{body}")
    return "\n\n".join(parts)
