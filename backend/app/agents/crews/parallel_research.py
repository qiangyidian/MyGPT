"""The parallel-research flow: Coordinator → (Web Researcher ‖ KB Researcher)
→ Analyst → Writer.

This profile exists to exercise genuine multi-agent concurrency so the
visualization can prove "multiple agents running at once" is real, not faked:

  * Stage 0 — Coordinator splits the question into two research lines.
  * Stage 1 — Web Researcher and KB Researcher run **concurrently** via
    ``asyncio.gather``. Both are ``running`` at the same time; each completes
    independently.
  * Stage 2 — Analyst is a **join**: it only starts once *both* researchers'
    handoff edges are ``completed``. The lifecycle emitter enforces this.
  * Stage 3 — Writer writes the cited answer from the analyst's finding.

Fail-fast policy (documented): if either researcher fails, the gather raises
and the runtime cancels downstream nodes via
:meth:`AgentLifecycleEmitter.cancel_downstream`. The other in-flight researcher
is awaited (its result is discarded) so we never leak a task.
"""
from __future__ import annotations

import logging
from typing import Any

from app.agents.crews.stage import StageSpec
from app.agents.graph import AgentGraph, build_parallel_research_graph

logger = logging.getLogger(__name__)


_COORDINATOR_BACKSTORY = (
    "You are a coordination agent. Given a question, you split it into a web "
    "research line and a knowledge-base research line, returning both sub-queries."
)
_WEB_RESEARCHER_BACKSTORY = (
    "You are a web researcher. You gather external evidence using web_search / "
    "http_get. Never fabricate sources."
)
_KB_RESEARCHER_BACKSTORY = (
    "You are a knowledge-base researcher. You retrieve internal documents using "
    "the file_analyze tool. Never fabricate sources."
)


def build_parallel_research_stages(
    *,
    llm: Any,
    tools: list[Any],
    question: str,
) -> tuple[AgentGraph, list[StageSpec]]:
    """Build the parallel-research flow.

    ``tools`` is shared across all agents that need tools (coordinator and
    writer get none). Returns the graph + stage specs; the runtime groups
    same-stage specs and runs them concurrently.
    """
    from crewai import Agent, Task

    coordinator = Agent(
        role="Coordinator",
        goal="Split the question into a web line and a KB line.",
        backstory=_COORDINATOR_BACKSTORY,
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )
    web_researcher = Agent(
        role="Web Researcher",
        goal="Gather external evidence for the web research line.",
        backstory=_WEB_RESEARCHER_BACKSTORY,
        llm=llm,
        tools=tools or None,
        allow_delegation=False,
        verbose=False,
    )
    kb_researcher = Agent(
        role="KB Researcher",
        goal="Gather internal evidence for the KB research line.",
        backstory=_KB_RESEARCHER_BACKSTORY,
        llm=llm,
        tools=tools or None,
        allow_delegation=False,
        verbose=False,
    )
    analyst = Agent(
        role="Analyst",
        goal="Merge and cross-check evidence from both research lines.",
        backstory=(
            "You are a skeptical analyst. You merge the two research lines' "
            "evidence, cross-check, flag conflicts, and judge sufficiency."
        ),
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )
    writer = Agent(
        role="Writer",
        goal="Write the final cited answer from verified evidence only.",
        backstory=(
            "You are a clear writer. You produce the final answer using ONLY "
            "verified evidence in context. Cite with [source N]."
        ),
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )

    coord_task = Task(
        description=(
            f"Question: {question}\n\nSplit this into two research lines: "
            "(1) a web_search query for external evidence, (2) a knowledge-base "
            "query for internal docs. Return both sub-queries."
        ),
        expected_output="Two sub-queries: one for web, one for the knowledge base.",
        agent=coordinator,
    )
    web_task = Task(
        description=(
            "Using the coordinator's web research line in context, gather "
            "external evidence with web_search / http_get. Report sources + snippets."
        ),
        expected_output="A list of {source, snippet} evidence items from the web.",
        agent=web_researcher,
    )
    kb_task = Task(
        description=(
            "Using the coordinator's KB research line in context, retrieve "
            "internal documents with file_analyze. Report sources + snippets."
        ),
        expected_output="A list of {source, snippet} evidence items from the KB.",
        agent=kb_researcher,
    )
    analyze_task = Task(
        description=(
            "Merge the web and KB evidence in context. Cross-check, flag "
            "conflicts, list verified facts, and judge sufficiency for: "
            f"{question}"
        ),
        expected_output="A structured finding: sufficient, conflicts, verified_facts, conclusion.",
        agent=analyst,
    )
    write_task = Task(
        description=(
            "Using ONLY the verified evidence in context, write the final "
            f"answer to: {question}. Cite with [source N]."
        ),
        expected_output="A concise, well-structured, cited final answer.",
        agent=writer,
    )

    graph = build_parallel_research_graph(question)
    stages = [
        StageSpec(agent_id="coordinator", agent=coordinator, task=coord_task, depends_on=[], stage=0),
        StageSpec(agent_id="web-researcher", agent=web_researcher, task=web_task, depends_on=["coordinator"], stage=1),
        StageSpec(agent_id="kb-researcher", agent=kb_researcher, task=kb_task, depends_on=["coordinator"], stage=1),
        StageSpec(agent_id="analyst", agent=analyst, task=analyze_task, depends_on=["web-researcher", "kb-researcher"], stage=2),
        StageSpec(agent_id="writer", agent=writer, task=write_task, depends_on=["analyst"], stage=3),
    ]
    return graph, stages
