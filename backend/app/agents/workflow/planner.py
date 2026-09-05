"""Plan construction, validation, and revision (Task 6).

:func:`validate_plan` rejects cycles, missing dependencies, and duplicate ids
(topological sort). The template builders :func:`build_plan_for_profile`
construct declarative :class:`~app.agents.workflow.schemas.Plan` objects that
mirror the existing static graph topology in :mod:`app.agents.graph`
(:func:`build_deep_research_graph`, :func:`build_parallel_research_graph`,
:func:`build_debate_graph`) — so the same profiles render as either a graph or
a verifiable plan.

:func:`revise_plan` produces a NEW versioned plan after a ``revise`` verdict:
it RETAINS completed valid work (steps not flagged, with their observations
carried over as ``skip``) and marks only the flagged steps for re-execution.
"""
from __future__ import annotations

from app.agents.workflow.schemas import (
    Plan,
    PlanValidationError,
    RetryPolicy,
    Step,
    StepObservation,
)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_plan(plan: Plan) -> None:
    """Reject cycles, missing deps, and duplicate ids. Raises on the first
    violation found; returns ``None`` when the plan is a valid DAG."""
    plan.validate()


# --------------------------------------------------------------------------- #
# Default retry policy shared by the templates' tool-calling steps
# --------------------------------------------------------------------------- #
_TRANSIENT = RetryPolicy(
    max_retries=1,
    transient_errors=("timeout", "temporarily", "rate limit", "503", "502", "connection"),
)


# --------------------------------------------------------------------------- #
# Templates — mirror the existing graph builders
# --------------------------------------------------------------------------- #
def build_deep_research_plan(question: str) -> Plan:
    """Researcher -> Analyst -> Writer (sequential deps).

    Mirrors :func:`app.agents.graph.build_deep_research_graph`. Each step
    depends on the prior one, so the ready set is always a singleton and the
    plan runs sequentially (max_concurrency == 1).
    """
    q = (question or "").strip()
    return Plan(
        version=1,
        goal=q,
        profile="deep_research",
        max_replans=1,
        steps=[
            Step(
                id="researcher",
                role="researcher",
                name="Researcher",
                task_description=f"Decompose and gather evidence for: {q}",
                dependencies=[],
                tool_allowlist=["web_search", "http_get", "file_analyze"],
                retry_policy=_TRANSIENT,
                acceptance_criteria={"min_chars": 1},
            ),
            Step(
                id="analyst",
                role="analyst",
                name="Analyst",
                task_description="Cross-check the researcher's evidence for sufficiency.",
                dependencies=["researcher"],
                acceptance_criteria={"min_chars": 1},
            ),
            Step(
                id="writer",
                role="writer",
                name="Writer",
                task_description=f"Write the cited final answer to: {q}",
                dependencies=["analyst"],
                acceptance_criteria={"min_chars": 1},
            ),
        ],
    )


def build_parallel_research_plan(question: str) -> Plan:
    """Coordinator -> (Web Researcher || KB Researcher) -> Analyst -> Writer.

    Mirrors :func:`app.agents.graph.build_parallel_research_graph`. The two
    researchers depend only on the coordinator (not on each other) so after the
    coordinator completes they are both in the ready set and the engine runs
    them concurrently (max_concurrency == 2). The Analyst is a JOIN on both.
    """
    q = (question or "").strip()
    return Plan(
        version=1,
        goal=q,
        profile="parallel_research",
        max_replans=1,
        steps=[
            Step(
                id="coordinator",
                role="coordinator",
                name="Coordinator",
                task_description=f"Split the question into web + KB lines: {q}",
                dependencies=[],
                acceptance_criteria={"min_chars": 1},
            ),
            Step(
                id="web-researcher",
                role="researcher",
                name="Web Researcher",
                task_description="Gather external evidence via web_search / http_get.",
                dependencies=["coordinator"],
                tool_allowlist=["web_search", "http_get"],
                retry_policy=_TRANSIENT,
                acceptance_criteria={"min_chars": 1},
            ),
            Step(
                id="kb-researcher",
                role="researcher",
                name="KB Researcher",
                task_description="Gather internal evidence via file_analyze (RAG).",
                dependencies=["coordinator"],
                tool_allowlist=["file_analyze"],
                retry_policy=_TRANSIENT,
                acceptance_criteria={"min_chars": 1},
            ),
            Step(
                id="analyst",
                role="analyst",
                name="Analyst",
                task_description="Merge and cross-check both research lines.",
                dependencies=["web-researcher", "kb-researcher"],
                acceptance_criteria={"min_chars": 1},
            ),
            Step(
                id="writer",
                role="writer",
                name="Writer",
                task_description=f"Write the cited final answer to: {q}",
                dependencies=["analyst"],
                acceptance_criteria={"min_chars": 1},
            ),
        ],
    )


def build_debate_plan(question: str) -> Plan:
    """Advocate-A || Advocate-B -> Judge (join on both).

    Mirrors :func:`app.agents.graph.build_debate_graph`. The two advocates have
    no dependencies on each other so they run concurrently; the Judge is a JOIN
    that reads both. Candidate sides are extracted from the question (any A-vs-B
    pair works; falls back to A/B).
    """
    from app.agents.planning import extract_debate_sides

    sides = extract_debate_sides(question or "")
    sa = (sides.side_a if sides else "A").strip() or "A"
    sb = (sides.side_b if sides else "B").strip() or "B"
    return Plan(
        version=1,
        goal=(question or "").strip(),
        profile="debate",
        max_replans=1,
        steps=[
            Step(
                id="advocate-a",
                role="advocate",
                name=f"{sa} Advocate",
                task_description=f"Build the strongest structured case for {sa}.",
                dependencies=[],
                acceptance_criteria={"min_chars": 1},
            ),
            Step(
                id="advocate-b",
                role="advocate",
                name=f"{sb} Advocate",
                task_description=f"Build the strongest structured case for {sb}.",
                dependencies=[],
                acceptance_criteria={"min_chars": 1},
            ),
            Step(
                id="judge",
                role="judge",
                name="Judge",
                task_description=f"Weigh {sa} vs {sb} on the same dimensions; conditional verdict.",
                dependencies=["advocate-a", "advocate-b"],
                acceptance_criteria={"min_chars": 1},
            ),
        ],
    )


def build_plan_for_profile(profile: str, question: str) -> Plan:
    """Pick a plan template by profile. Mirrors
    :func:`app.agents.graph.build_graph_for_profile`."""
    if profile == "parallel_research":
        return build_parallel_research_plan(question)
    if profile == "debate":
        return build_debate_plan(question)
    # default + "deep_research"
    return build_deep_research_plan(question)


# --------------------------------------------------------------------------- #
# Revision — retain completed valid work, rework only flagged steps
# --------------------------------------------------------------------------- #
def revise_plan(
    plan: Plan,
    revise_step_ids: list[str],
    observations: dict[str, StepObservation],
) -> Plan:
    """Return a NEW versioned plan that reworks only ``revise_step_ids``.

    Every step NOT flagged keeps its observation and is marked ``skip`` so the
    engine does not re-execute it; the flagged steps are re-run. The plan's
    ``version`` increments and ``replan_count`` advances by one. The revised
    plan is validated before being returned.
    """
    revise_set = set(revise_step_ids or [])
    # Reject verifier bugs early: an unknown revise id would otherwise mark every
    # step ``skip`` and silently loop verify→revise until max_replans exhausts.
    step_ids = {s.id for s in plan.steps}
    unknown = revise_set - step_ids
    if unknown:
        raise PlanValidationError(
            f"revise_step_ids reference unknown step(s): {sorted(unknown)}"
        )
    carried: dict[str, StepObservation] = {}
    new_steps: list[Step] = []
    for s in plan.steps:
        if s.id in revise_set:
            # Rework: a fresh, runnable copy (skip stays False).
            new_steps.append(s.model_copy(update={"skip": False}))
        else:
            # Retain: carry its observation and mark skipped.
            new_steps.append(s.model_copy(update={"skip": True}))
            if s.id in observations:
                carried[s.id] = observations[s.id]

    revised = Plan(
        version=plan.version + 1,
        goal=plan.goal,
        profile=plan.profile,
        steps=new_steps,
        replan_count=plan.replan_count + 1,
        max_replans=plan.max_replans,
        carry_observations=carried,
    )
    validate_plan(revised)
    return revised
