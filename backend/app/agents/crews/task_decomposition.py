"""任务分解拓扑：Coordinator → Worker ×N（并行） → Integrator。

这是唯一一个 worker 数量**动态**的 profile：协调者拆出 N 个工作项，N 个
worker 无依赖地并行执行，整合者全量汇合。

三个角色的 backstory 都把「不要越界」写进规则 —— 多 Agent 最常见的失败是
worker 互相重复、整合者重新做一遍而不是汇合。
"""
from __future__ import annotations

from typing import Any

from app.agents.crews.stage import StageSpec
from app.agents.graph import AgentGraph, _clamp_workers, build_task_decomposition_graph

_DECOMPOSER_BACKSTORY = (
    "You are a task coordinator. Rules:\n"
    "1) Break the user's request into {n} work items that are genuinely "
    "INDEPENDENT — no item should need another's result.\n"
    "2) Each item must be concrete enough that a worker can complete it "
    "without asking follow-up questions.\n"
    "3) Do NOT do the work yourself and do NOT produce the final answer.\n"
    "4) Output a numbered list; item i will be handed to Worker i.\n"
    "5) Answer in the user's language."
)

_WORKER_BACKSTORY = (
    "You are worker {i} of {n} independent workers. Rules:\n"
    "1) Complete ONLY the work item assigned to you — do not attempt other "
    "items, and do not write the final deliverable.\n"
    "2) Your output must be SELF-CONTAINED: the integrator sees only your "
    "result, not your reasoning.\n"
    "3) Do not fabricate unverifiable facts. Where unsure, say so.\n"
    "4) Answer in the user's language."
)

_INTEGRATOR_BACKSTORY = (
    "You are the integrator. Every worker's output is in your context. Rules:\n"
    "1) MERGE the workers' outputs — do not redo their work from scratch.\n"
    "2) Resolve conflicts explicitly: say which version you kept and why.\n"
    "3) Remove duplication; the result must read as ONE deliverable.\n"
    "4) If a worker's output is missing or clearly unusable, say so rather "
    "than silently papering over it.\n"
    "5) Answer in the user's language, in well-structured Markdown."
)


def build_task_decomposition_stages(
    *, llm: Any, tools: list[Any], question: str, worker_count: int = 3
) -> tuple[AgentGraph, list[StageSpec]]:
    """Build the decomposition flow. ``tools`` is unused (matches the signature
    of the other crew builders so the runtime can dispatch uniformly)."""
    from crewai import Agent, Task

    n = _clamp_workers(worker_count)

    decomposer = Agent(
        role="Coordinator",
        goal=f"Split the request into {n} independent work items.",
        backstory=_DECOMPOSER_BACKSTORY.format(n=n),
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )
    integrator = Agent(
        role="Integrator",
        goal="Merge every worker's output into one coherent deliverable.",
        backstory=_INTEGRATOR_BACKSTORY,
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )

    stages: list[StageSpec] = [
        StageSpec(
            agent_id="decomposer",
            agent=decomposer,
            task=Task(
                description=(
                    f"User request: {question}\n\n"
                    f"Break it into {n} independent work items. Output a "
                    "numbered list."
                ),
                expected_output="A numbered list of independent work items.",
                agent=decomposer,
            ),
            depends_on=[],
            stage=0,
        )
    ]

    worker_ids: list[str] = []
    for i in range(1, n + 1):
        wid = f"worker-{i}"
        worker_ids.append(wid)
        worker = Agent(
            role=f"Worker {i}",
            goal=f"Complete work item {i} from the coordinator's breakdown.",
            backstory=_WORKER_BACKSTORY.format(i=i, n=n),
            llm=llm,
            allow_delegation=False,
            verbose=False,
        )
        stages.append(
            StageSpec(
                agent_id=wid,
                agent=worker,
                task=Task(
                    description=(
                        f"User request: {question}\n\n"
                        f"You are Worker {i}. The coordinator split the request "
                        "into numbered items (in your context). Complete item "
                        f"{i} only, and produce a self-contained result."
                    ),
                    expected_output="A self-contained result for this work item.",
                    agent=worker,
                ),
                depends_on=["decomposer"],
                stage=1,
            )
        )

    stages.append(
        StageSpec(
            agent_id="integrator",
            agent=integrator,
            task=Task(
                description=(
                    f"User request: {question}\n\n"
                    "Every worker's result is in your context. Merge them into "
                    "one coherent deliverable, resolving conflicts explicitly."
                ),
                expected_output="One merged, coherent Markdown deliverable.",
                agent=integrator,
            ),
            depends_on=worker_ids,
            stage=2,
        )
    )

    graph = build_task_decomposition_graph(question, worker_count=n)
    return graph, stages
