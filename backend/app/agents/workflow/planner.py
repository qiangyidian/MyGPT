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
# Default retry policy shared by every template step
#
# 每个步骤都是一次真实的模型调用，所以限流/502/超时对 analyst、writer、judge
# 一视同仁 —— 只给带工具的步骤配回退，等于让一处瞬时的模型端点故障把整轮
# 直接判死。
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
                retry_policy=_TRANSIENT,
            ),
            Step(
                id="writer",
                role="writer",
                name="Writer",
                task_description=f"Write the cited final answer to: {q}",
                dependencies=["analyst"],
                acceptance_criteria={"min_chars": 1},
                retry_policy=_TRANSIENT,
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
                retry_policy=_TRANSIENT,
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
                retry_policy=_TRANSIENT,
            ),
            Step(
                id="writer",
                role="writer",
                name="Writer",
                task_description=f"Write the cited final answer to: {q}",
                dependencies=["analyst"],
                acceptance_criteria={"min_chars": 1},
                retry_policy=_TRANSIENT,
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
                retry_policy=_TRANSIENT,
            ),
            Step(
                id="advocate-b",
                role="advocate",
                name=f"{sb} Advocate",
                task_description=f"Build the strongest structured case for {sb}.",
                dependencies=[],
                acceptance_criteria={"min_chars": 1},
                retry_policy=_TRANSIENT,
            ),
            Step(
                id="judge",
                role="judge",
                name="Judge",
                task_description=f"Weigh {sa} vs {sb} on the same dimensions; conditional verdict.",
                dependencies=["advocate-a", "advocate-b"],
                retry_policy=_TRANSIENT,
                acceptance_criteria={"min_chars": 1},
            ),
        ],
    )


def build_task_decomposition_plan(question: str, worker_count: int = 3) -> Plan:
    """Coordinator → Worker ×N（并行） → Integrator（全量 join）。

    worker 之间无依赖，所以 decomposer 完成后它们同时在 ready 集里，
    引擎会真并行执行（max_concurrency == N）。
    """
    from app.agents.graph import _clamp_workers

    q = (question or "").strip()
    n = _clamp_workers(worker_count)
    steps = [
        Step(
            id="decomposer",
            role="coordinator",
            name="Coordinator",
            task_description=(
                f"Break the request into {n} independent, parallelisable work "
                f"items: {q}"
            ),
            dependencies=[],
            retry_policy=_TRANSIENT,
            acceptance_criteria={"min_chars": 1},
        )
    ]
    for i in range(1, n + 1):
        steps.append(
            Step(
                id=f"worker-{i}",
                role="worker",
                name=f"Worker {i}",
                task_description=(
                    f"Complete work item {i} from the coordinator's breakdown. "
                    "Produce a self-contained result that needs no further work."
                ),
                dependencies=["decomposer"],
                retry_policy=_TRANSIENT,
                acceptance_criteria={"min_chars": 1},
            )
        )
    steps.append(
        Step(
            id="integrator",
            role="integrator",
            name="Integrator",
            task_description=(
                "Merge every worker's output into one coherent deliverable; "
                "resolve conflicts and remove duplication."
            ),
            dependencies=[f"worker-{i}" for i in range(1, n + 1)],
            retry_policy=_TRANSIENT,
            acceptance_criteria={"min_chars": 1},
        )
    )
    return Plan(
        version=1, goal=q, profile="task_decomposition",
        steps=steps, max_replans=1,
    )


def build_write_review_plan(question: str) -> Plan:
    """Draft → Review → Finalize（严格串行）。"""
    q = (question or "").strip()
    return Plan(
        version=1, goal=q, profile="write_review",
        max_replans=1,
        steps=[
            Step(
                id="drafter", role="drafter", name="Drafter",
                task_description=f"Write a complete first draft for: {q}",
                dependencies=[],
                acceptance_criteria={"min_chars": 1},
                retry_policy=_TRANSIENT,
            ),
            Step(
                id="reviewer", role="reviewer", name="Reviewer",
                task_description=(
                    "Review the draft against the request. Output a STRUCTURED "
                    "list of concrete problems (factual, logical, structural, "
                    "clarity). Do NOT rewrite the draft yourself."
                ),
                dependencies=["drafter"],
                acceptance_criteria={"min_chars": 1},
                retry_policy=_TRANSIENT,
            ),
            Step(
                id="finalizer", role="finalizer", name="Finalizer",
                task_description=(
                    "Produce the final version, addressing every point in the "
                    "reviewer's list. Keep what was already good."
                ),
                dependencies=["reviewer"],
                acceptance_criteria={"min_chars": 1},
                retry_policy=_TRANSIENT,
            ),
        ],
    )


def build_plan_for_profile(profile: str, question: str) -> Plan:
    """Pick a plan template by profile. Mirrors
    :func:`app.agents.graph.build_graph_for_profile`."""
    if profile == "parallel_research":
        return build_parallel_research_plan(question)
    if profile == "task_decomposition":
        return build_task_decomposition_plan(question)
    if profile == "write_review":
        return build_write_review_plan(question)
    if profile == "debate":
        return build_debate_plan(question)
    # default + "deep_research"
    return build_deep_research_plan(question)


# --------------------------------------------------------------------------- #
# Revision — retain completed valid work, rework only flagged steps
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Plan -> 前端 PlanReview 卡片
# --------------------------------------------------------------------------- #
# 引擎路径以前不发计划事件：面板上的计划卡、确认/修改入口在引擎轮次里整个消失
# （验收 §11.3 要求「计划总是发布且可修改」）。
_ROLE_TITLES = {
    "researcher": "检索相关资料",
    "coordinator": "拆分任务与调度",
    "web-researcher": "检索网络资料",
    "kb-researcher": "检索知识库",
    "analyst": "核对来源与差异",
    "writer": "生成带引用的汇总",
    "advocate-a": "正方论证",
    "advocate-b": "反方论证",
    "judge": "裁判权衡与结论",
    "decomposer": "拆分独立工作项",
    "worker": "完成分派的工作项",
    "integrator": "汇总为一份交付物",
    "drafter": "起草初稿",
    "reviewer": "审阅并列出问题",
    "finalizer": "按审阅意见定稿",
}

_PROFILE_SUMMARIES = {
    "deep_research": "先检索资料，再交叉核对来源，最后生成带引用的汇总",
    "parallel_research": "网络与知识库两路并行检索，再汇总核对并成文",
    "debate": "正反双方各自论证，由裁判在同一维度上权衡并给出结论",
    "task_decomposition": "把请求拆成独立工作项并行完成，再整合为一份交付物",
    "write_review": "先起草，再由审阅者只诊断不改写，最后按意见定稿",
}


def _step_title(step: Step) -> str:
    return _ROLE_TITLES.get(step.role) or _ROLE_TITLES.get(step.id) or step.name or step.id


def plan_summary_for_profile(profile: str, question: str) -> str:
    """One-line, user-facing description of what will run."""
    known = _PROFILE_SUMMARIES.get(profile)
    if known:
        return known
    goal = (question or "").strip()
    return f"按下列步骤完成：{goal[:120]}" if goal else "按下列步骤完成请求"


def plan_acceptance_criteria(question: str, step_count: int) -> list[str]:
    """Deterministic acceptance criteria, derived from the plan that will run."""
    criteria = [
        "回答直接针对用户问题，不偏题",
        "关键结论附带可核实的来源引用",
    ]
    if step_count:
        criteria.append(f"完成全部 {step_count} 个计划步骤")
    if question and len(question) > 30:
        criteria.append("对问题中的多个子点分别作答，不遗漏")
    criteria.append("明确区分事实与推断；无法核实的结论标注不确定性")
    return criteria


_WEB_TOOLS = {"web_search", "http_get", "web_fetch"}
_KB_TOOLS = {"kb_search", "knowledge_base_search", "rag_search"}


def _step_sources(step: Step) -> list[str]:
    """Which evidence lines a step draws on, inferred from its tool allowlist.

    An empty allowlist means "whatever the run's tool set provides" -> report
    both lines rather than none, so the card never under-claims coverage.
    """
    allowed = set(step.tool_allowlist or [])
    if not allowed:
        return ["knowledge_base", "web"]
    sources: list[str] = []
    if allowed & _WEB_TOOLS:
        sources.append("web")
    if allowed & _KB_TOOLS or not (allowed - _WEB_TOOLS):
        sources.append("knowledge_base")
    return sources


def plan_to_ui_payload(plan: Plan, *, requires_confirmation: bool = False) -> dict:
    """把引擎 :class:`Plan` 转成 walker 同形状的 UI 计划字典。"""
    question = (plan.goal or "").strip()
    steps = [
        {"id": s.id, "title": _step_title(s), "sources": _step_sources(s)}
        for s in plan.steps
    ]
    return {
        "summary": plan_summary_for_profile(plan.profile, question),
        "steps": steps,
        "acceptanceCriteria": plan_acceptance_criteria(question, len(steps)),
        "requires_confirmation": requires_confirmation,
    }


def _downstream_closure(steps: list[Step], roots: set[str]) -> set[str]:
    """Every step id that transitively depends on one of ``roots`` (excluding them)."""
    dependents: dict[str, list[str]] = {}
    for s in steps:
        for dep in s.dependencies:
            dependents.setdefault(dep, []).append(s.id)
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        for child in dependents.get(stack.pop(), []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen - roots


def revise_plan(
    plan: Plan,
    revise_step_ids: list[str],
    observations: dict[str, StepObservation],
) -> Plan:
    """Return a NEW versioned plan that reworks only ``revise_step_ids``.

    Every step NOT flagged (and not downstream of a flagged one) keeps its
    observation and is marked ``skip`` so the engine does not re-execute it.
    Flagged steps **and their transitive dependents** re-run — a dependent kept
    on the old upstream output would be stale forever, and the verifier would
    judge a plan whose final answer never reflects the revision. The plan's
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
    # 被改动步骤的**全部传递下游**也必须重跑：它们手上的观测是按旧上游算出
    # 的，留着不重算就等于让 verifier 复核一份永远不会更新的过期答案。
    stale = _downstream_closure(plan.steps, revise_set)
    must_rerun = revise_set | stale
    new_steps: list[Step] = []
    for s in plan.steps:
        if s.id in must_rerun:
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
