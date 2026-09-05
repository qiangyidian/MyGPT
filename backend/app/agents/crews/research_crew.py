"""The Researcher -> Analyst -> Writer research flow (Phase 4, refactored for
real per-agent lifecycle in the multi-agent visualization phase).

Three single-purpose CrewAI agents, each with a strict role so evidence
provenance is preserved and the Writer can't invent unverified facts:

  * **Researcher** — decomposes the question, calls search/http/RAG tools via
    the gateway adapters, returns raw *evidence* (sources + snippets).
  * **Analyst** — cross-checks the evidence, flags conflicts and gaps, judges
    whether the evidence is sufficient, returns a structured *finding*.
  * **Writer** — writes the final cited answer from the Analyst's finding.
    No tools, no new facts — it may only use verified evidence.

:func:`build_research_stages` returns the static graph + a list of
:class:`~app.agents.crews.stage.StageSpec` (one per agent). The runtime
executes each spec via ``Agent.aexecute_task`` and feeds prior outputs in as
the task ``context`` string — so each agent's lifecycle (running/completed) is
real and the handoff edges fire exactly when the data is available.
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from app.agents.crews.stage import StageSpec
from app.agents.graph import AgentGraph, build_deep_research_graph

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Structured handoff schemas
# --------------------------------------------------------------------------- #
class EvidenceItem(BaseModel):
    source: str = Field(description="URL or document id the evidence came from")
    snippet: str = Field(description="The relevant excerpt")
    note: str = Field(default="", description="Why this is relevant")


class ResearchEvidence(BaseModel):
    sub_questions: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class ConflictNote(BaseModel):
    claim: str
    positions: list[str] = Field(default_factory=list, description="conflicting statements + their sources")


class AnalystFinding(BaseModel):
    sufficient: bool = Field(description="Is the evidence enough to answer?")
    conflicts: list[ConflictNote] = Field(default_factory=list)
    verified_facts: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    conclusion: str = ""


# --------------------------------------------------------------------------- #
# Prompt templates (kept explicit, not in YAML, so they live next to the code)
# --------------------------------------------------------------------------- #
_RESEARCHER_BACKSTORY = (
    "You are a meticulous researcher. You decompose a question into sub-questions "
    "and gather evidence using search and retrieval tools. You never fabricate "
    "sources — every evidence item must come from a real tool result. Prefer a "
    "few precise searches over one broad one."
)
_ANALYST_BACKSTORY = (
    "You are a skeptical analyst. You cross-check the researcher's evidence, "
    "flag conflicts and gaps, and judge whether the evidence is sufficient to "
    "answer. You do not introduce new facts; you only evaluate what's given."
)
_WRITER_BACKSTORY = (
    "You are a clear writer. You produce the final answer using ONLY the "
    "verified facts and evidence the analyst approved. You cite sources with "
    "[source N] markers. You never state a fact that wasn't in the evidence. "
    "If the evidence is insufficient, you say so honestly rather than guessing."
)


# --------------------------------------------------------------------------- #
# Stage builder (replaces the old single-Crew builder)
# --------------------------------------------------------------------------- #
def build_research_stages(
    *,
    llm: Any,
    tools: list[Any],
    question: str,
) -> tuple[AgentGraph, list[StageSpec]]:
    """Build the sequential Researcher -> Analyst -> Writer flow.

    Returns the static :class:`AgentGraph` (all nodes pending) plus one
    :class:`StageSpec` per agent. The runtime executes them in order, feeding
    each prior agent's raw output into the next as the task ``context``.
    """
    from crewai import Agent, Task

    researcher = Agent(
        role="Researcher",
        goal="Gather sufficient, well-sourced evidence to answer the question.",
        backstory=_RESEARCHER_BACKSTORY,
        llm=llm,
        tools=tools or None,
        allow_delegation=False,
        verbose=False,
    )
    analyst = Agent(
        role="Analyst",
        goal="Verify the evidence, flag conflicts, decide if it's sufficient.",
        backstory=_ANALYST_BACKSTORY,
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )
    writer = Agent(
        role="Writer",
        goal="Write the final cited answer from verified evidence only.",
        backstory=_WRITER_BACKSTORY,
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )

    research_task = Task(
        description=(
            f"Research question: {question}\n\n"
            "Decompose it into sub-questions, gather evidence with your tools, "
            "and report: the sub-questions, every evidence item with its source "
            "and a relevant snippet, and any gaps you couldn't fill."
        ),
        expected_output=(
            "Structured evidence: sub_questions, evidence list "
            "{source, snippet, note}, and gaps."
        ),
        agent=researcher,
    )
    analyze_task = Task(
        description=(
            "Review the evidence provided in context. Cross-check sources, flag "
            "conflicting claims (with the positions and their sources), list "
            "verified facts, note unresolved questions, and state whether the "
            f"evidence is sufficient to answer the original question: {question}"
        ),
        expected_output=(
            "Structured finding: sufficient (bool), conflicts, verified_facts, "
            "unresolved, conclusion."
        ),
        agent=analyst,
    )
    write_task = Task(
        description=(
            "Using ONLY the verified facts and approved evidence in context, "
            f"write the final answer to: {question}\n\n"
            "Cite each fact with [source N]. Do not add any fact not present in "
            "the evidence. If insufficient, say so explicitly."
        ),
        expected_output="A concise, well-structured, cited final answer.",
        agent=writer,
    )

    graph = build_deep_research_graph(question)
    stages = [
        StageSpec(agent_id="researcher", agent=researcher, task=research_task, depends_on=[], stage=0),
        StageSpec(agent_id="analyst", agent=analyst, task=analyze_task, depends_on=["researcher"], stage=1),
        StageSpec(agent_id="writer", agent=writer, task=write_task, depends_on=["analyst"], stage=2),
    ]
    return graph, stages


# --------------------------------------------------------------------------- #
# Back-compat: the old single-Crew builder (still used by tests that assert
# the three-role structure; the live runtime now uses build_research_stages).
# --------------------------------------------------------------------------- #
def build_research_crew(
    *,
    llm: Any,
    tools: list[Any],
    question: str,
    run_id: Any,
) -> Any:
    """Construct the sequential Researcher -> Analyst -> Writer Crew.

    .. deprecated::
       Kept for structural tests. The live multi-agent runtime uses
       :func:`build_research_stages` + explicit per-stage execution so that
       agent lifecycle events reflect real execution.
    """
    from crewai import Agent, Crew, Process, Task

    researcher = Agent(
        role="Researcher",
        goal="Gather sufficient, well-sourced evidence to answer the question.",
        backstory=_RESEARCHER_BACKSTORY,
        llm=llm,
        tools=tools or None,
        allow_delegation=False,
        verbose=False,
    )
    analyst = Agent(
        role="Analyst",
        goal="Verify the evidence, flag conflicts, decide if it's sufficient.",
        backstory=_ANALYST_BACKSTORY,
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )
    writer = Agent(
        role="Writer",
        goal="Write the final cited answer from verified evidence only.",
        backstory=_WRITER_BACKSTORY,
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )

    research_task = Task(
        description=(
            f"Research question: {question}\n\n"
            "Decompose it into sub-questions, gather evidence with your tools, "
            "and report: the sub-questions, every evidence item with its source "
            "and a relevant snippet, and any gaps you couldn't fill."
        ),
        expected_output=(
            "Structured evidence: sub_questions, evidence list "
            "{source, snippet, note}, and gaps."
        ),
        agent=researcher,
    )
    analyze_task = Task(
        description=(
            "Review the researcher's evidence above. Cross-check sources, flag "
            "conflicting claims (with the positions and their sources), list "
            "verified facts, note unresolved questions, and state whether the "
            "evidence is sufficient to answer the original question: "
            f"{question}"
        ),
        expected_output=(
            "Structured finding: sufficient (bool), conflicts, verified_facts, "
            "unresolved, conclusion."
        ),
        agent=analyst,
        context=[research_task],
    )
    write_task = Task(
        description=(
            "Using ONLY the analyst's verified facts and approved evidence above, "
            f"write the final answer to: {question}\n\n"
            "Cite each fact with [source N]. Do not add any fact not present in "
            "the evidence. If insufficient, say so explicitly."
        ),
        expected_output="A concise, well-structured, cited final answer.",
        agent=writer,
        context=[research_task, analyze_task],
    )

    return Crew(
        agents=[researcher, analyst, writer],
        tasks=[research_task, analyze_task, write_task],
        process=Process.sequential,
        memory=False,  # tests/prod run without Redis-backed crew memory
        verbose=False,
    )


# --------------------------------------------------------------------------- #
# Reviewer quality gate (post-run, lightweight)
# --------------------------------------------------------------------------- #
def review_crew_output(raw_answer: str, evidence: ResearchEvidence | None) -> dict[str, Any]:
    """Lightweight reviewer: does the answer cite sources and avoid obvious gaps?

    This is a structural check (not an LLM call) so it's deterministic and fast:
    the answer should contain at least one ``[source`` marker, and if evidence
    had gaps flagged, the answer should acknowledge them.
    """
    has_citation = "[source" in (raw_answer or "").lower()
    had_gaps = bool(evidence and evidence.gaps)
    mentions_limitation = any(
        w in (raw_answer or "").lower()
        for w in ("insufficient", "不足", "无法确认", "could not", "unclear", "不确定")
    )
    passed = has_citation and (not had_gaps or mentions_limitation)
    return {
        "passed": passed,
        "has_citation": has_citation,
        "had_gaps": had_gaps,
        "mentions_limitation": mentions_limitation,
        "note": (
            "answer cites sources and handles evidence gaps"
            if passed
            else "answer may lack citations or ignore evidence gaps"
        ),
    }
