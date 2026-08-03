"""Debate flow: Advocate-A ‖ Advocate-B → Judge.

A REAL multi-agent debate (not a single model role-playing several roles). Two
advocates run **concurrently** at the same stage — each argues ONLY for its
assigned candidate. The Judge is a **join**: it starts only after BOTH advocates
complete, reads both structured arguments, and returns a conditional verdict.

The two candidates are extracted from the user's question (via
:func:`app.agents.planning.extract_debate_sides`) so any A-vs-B pair works:
Python vs Go, React vs Vue, 微服务 vs 单体架构, PostgreSQL vs MySQL, etc. Nothing
is hardcoded to a specific pair.

Node ids (``advocate-a`` / ``advocate-b`` / ``judge``) are STABLE; only the
display names carry the candidate names. This keeps the FE graph reducer and
the persisted ``graph_state`` deterministic.
"""
from __future__ import annotations

import logging
from typing import Any

from app.agents.crews.stage import StageSpec
from app.agents.graph import AgentGraph, build_debate_graph
from app.agents.planning import DebateSubjects, extract_debate_sides

logger = logging.getLogger(__name__)

_ADVOCATE_BACKSTORY = (
    "You are an advocate agent in a structured debate. You argue ONLY for your "
    "assigned candidate ({side}). Rules:\n"
    "1) Build the strongest case for {side} from best practices, applicable "
    "conditions, cost, ecosystem, maintainability, performance, and delivery risk.\n"
    "2) Do NOT fabricate unverifiable facts. Where unsure, say so.\n"
    "3) Frankly acknowledge {side}'s limitations and where it is a poor fit.\n"
    "4) Do NOT deliver a verdict and do NOT attack the user or the other side.\n"
    "5) Do NOT converse with the other agent — produce your independent brief.\n"
    "6) Output a concise structured brief the Judge can consume.\n"
    "7) Answer in the user's language."
)

_ADVOCATE_EXPECTED = (
    "A JSON object: "
    '{"side": "<your candidate>", "key_arguments": [...], "limitations": [...], '
    '"best_fit_scenarios": [...], "risks": [...], "summary": "<one paragraph>"}.'
)

_JUDGE_BACKSTORY = (
    "You are a neutral Judge in a structured debate. Both advocates' briefs are "
    "in your context. Rules:\n"
    "1) Evaluate BOTH sides on the SAME dimensions (fit, cost, ecosystem, "
    "performance, maintainability, risk).\n"
    "2) Distinguish facts from speculation from preference.\n"
    "3) State clearly under which conditions {a} is better and under which {b} "
    "is better.\n"
    "4) Give a conditional conclusion tied to the user's actual goal.\n"
    "5) Do NOT decide by popularity alone and do NOT invent facts not present "
    "in the advocates' outputs. If evidence is insufficient, say so.\n"
    "6) Do NOT reveal internal chain-of-thought — only the public verdict.\n"
    "7) Answer in the user's language, in well-structured Markdown."
)


def _extract_sides(question: str) -> DebateSubjects:
    sides = extract_debate_sides(question)
    if sides is None or not sides.side_a or not sides.side_b:
        logger.warning("debate profile but no two candidates parsed; using A/B")
        return DebateSubjects(side_a="A", side_b="B")
    return sides


def build_debate_stages(
    *, llm: Any, tools: list[Any], question: str
) -> tuple[AgentGraph, list[StageSpec]]:
    """Build the debate flow: two parallel advocates → judge join.

    ``tools`` is accepted for signature parity with the research builders but
    unused (debate is structured argumentation, not tool-calling).
    """
    from crewai import Agent, Task

    sides = _extract_sides(question)
    sa, sb = sides.side_a, sides.side_b

    advocate_a = Agent(
        role=f"{sa} Advocate",
        goal=f"Build the strongest structured case for {sa}; no verdict.",
        backstory=_ADVOCATE_BACKSTORY.format(side=sa),
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )
    advocate_b = Agent(
        role=f"{sb} Advocate",
        goal=f"Build the strongest structured case for {sb}; no verdict.",
        backstory=_ADVOCATE_BACKSTORY.format(side=sb),
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )
    judge = Agent(
        role="Judge",
        goal=f"Weigh {sa} vs {sb} on the same dimensions and give a conditional verdict.",
        backstory=_JUDGE_BACKSTORY.format(a=sa, b=sb),
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )

    a_task = Task(
        description=(
            f"User's question: {question}\n\n"
            f"You are advocating for {sa} (against {sb}). Produce your structured brief."
        ),
        expected_output=_ADVOCATE_EXPECTED,
        agent=advocate_a,
    )
    b_task = Task(
        description=(
            f"User's question: {question}\n\n"
            f"You are advocating for {sb} (against {sa}). Produce your structured brief."
        ),
        expected_output=_ADVOCATE_EXPECTED,
        agent=advocate_b,
    )
    judge_task = Task(
        description=(
            f"User's question: {question}\n\n"
            "Both advocates' structured briefs are in context. Compare them on the "
            f"same dimensions, state when {sa} fits better and when {sb} fits better, "
            "and give a conditional conclusion for the user's goal."
        ),
        expected_output="A clear Markdown verdict: comparison table + conditional conclusion.",
        agent=judge,
    )

    graph = build_debate_graph(sa, sb)
    stages = [
        StageSpec(agent_id="advocate-a", agent=advocate_a, task=a_task, depends_on=[], stage=0),
        StageSpec(agent_id="advocate-b", agent=advocate_b, task=b_task, depends_on=[], stage=0),
        StageSpec(agent_id="judge", agent=judge, task=judge_task,
                  depends_on=["advocate-a", "advocate-b"], stage=1),
    ]
    return graph, stages
