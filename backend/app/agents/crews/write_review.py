"""写-审-改拓扑：Drafter → Reviewer → Finalizer（严格串行）。

质量保障来自**第二次触碰**：审阅者被明确禁止自己写终稿，只输出结构化问题
清单；定稿者据其逐条修改。若审阅者直接给"改好的版本"，这个拓扑就退化成
一次串行改写，三跳的价值全部蒸发。
"""
from __future__ import annotations

from typing import Any

from app.agents.crews.stage import StageSpec
from app.agents.graph import AgentGraph, build_write_review_graph

_DRAFTER_BACKSTORY = (
    "You are a drafting specialist. Rules:\n"
    "1) Produce a COMPLETE first draft that could ship as-is if nobody "
    "reviewed it — do not leave placeholders or TODO notes.\n"
    "2) Structure it clearly: it will be reviewed section by section.\n"
    "3) Do not fabricate unverifiable facts.\n"
    "4) Answer in the user's language."
)

_REVIEWER_BACKSTORY = (
    "You are a strict reviewer. Rules:\n"
    "1) Output a STRUCTURED list of concrete problems, grouped by kind: "
    "factual errors, logical gaps, structural problems, unclear passages.\n"
    "2) Each item must say WHAT is wrong and WHY it matters.\n"
    "3) Do NOT rewrite the draft and do NOT produce a corrected version — "
    "your job is diagnosis, not treatment.\n"
    "4) If a section is genuinely fine, say so rather than inventing problems.\n"
    "5) Answer in the user's language."
)

_FINALIZER_BACKSTORY = (
    "You are the finalizer. The draft and the reviewer's problem list are both "
    "in your context. Rules:\n"
    "1) Address EVERY item in the reviewer's list. If you disagree with one, "
    "say so explicitly and keep your version.\n"
    "2) Preserve what was already good — do not rewrite for the sake of it.\n"
    "3) The result must be a finished deliverable, not a revision memo.\n"
    "4) Answer in the user's language."
)


def build_write_review_stages(
    *, llm: Any, tools: list[Any], question: str
) -> tuple[AgentGraph, list[StageSpec]]:
    """Build the write-review flow. ``tools`` is unused (signature parity)."""
    from crewai import Agent, Task

    drafter = Agent(
        role="Drafter",
        goal="Produce a complete, reviewable first draft.",
        backstory=_DRAFTER_BACKSTORY,
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )
    reviewer = Agent(
        role="Reviewer",
        goal="Diagnose concrete problems in the draft; do not rewrite it.",
        backstory=_REVIEWER_BACKSTORY,
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )
    finalizer = Agent(
        role="Finalizer",
        goal="Produce the final version, addressing every review point.",
        backstory=_FINALIZER_BACKSTORY,
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )

    graph = build_write_review_graph(question)
    stages = [
        StageSpec(
            agent_id="drafter",
            agent=drafter,
            task=Task(
                description=(
                    f"User request: {question}\n\n"
                    "Produce a complete first draft. It will be reviewed next."
                ),
                expected_output="A complete, structured first draft.",
                agent=drafter,
            ),
            depends_on=[],
            stage=0,
        ),
        StageSpec(
            agent_id="reviewer",
            agent=reviewer,
            task=Task(
                description=(
                    f"User request: {question}\n\n"
                    "The draft is in your context. Output a structured list of "
                    "concrete problems. Do NOT rewrite it."
                ),
                expected_output=(
                    "A structured problem list: factual / logical / structural "
                    "/ clarity, each with what and why."
                ),
                agent=reviewer,
            ),
            depends_on=["drafter"],
            stage=1,
        ),
        StageSpec(
            agent_id="finalizer",
            agent=finalizer,
            task=Task(
                description=(
                    f"User request: {question}\n\n"
                    "The draft and the reviewer's problem list are in context. "
                    "Produce the finished deliverable, addressing every point."
                ),
                expected_output="The finished deliverable in Markdown.",
                agent=finalizer,
            ),
            depends_on=["reviewer"],
            stage=2,
        ),
    ]
    return graph, stages
