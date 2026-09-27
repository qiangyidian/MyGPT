"""Workflow topology：每个 profile 的拓扑**唯一声明处**（B15）。

在此之前的状况：「某个 profile 有哪些 stage、长什么形状、能不能跑多
Agent」这件事被硬编码在五处 —— :func:`app.agents.graph.build_graph_for_profile`、
:func:`app.agents.workflow.planner.build_plan_for_profile`、
``ChatOrchestrator._build_stage_adapter`` 的 builders 字典、
``CrewAIRuntime._run_multi_agent`` 的 if/elif、以及
``crewai_runtime._MULTI_AGENT_PROFILES``。五份清单各自演化，直接后果就是
B14 报的那类不一致：路由可以选出 ``write_review``，orchestrator 声称
``multi_agent_executed=True``，而 walker 的 if/elif 没有这一支，于是它静默
退化成单 Agent。

本模块把这件事收成一份 :class:`TopologySpec`，并且提供
:func:`evaluate_topology` —— 一个**纯函数**，输入 profile（可选 plan），
输出这套拓扑的自描述（跑哪些 stage、什么形状、几轮返修、缺什么能力），
供 eval / 运维面板 / 启动日志报告，不需要真跑一次才知道形状。

这里**不新增任何策略**：spec 里的每一条都只是把既有 builder 的事实写下来。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

# stage 在拓扑里的执行形状。
SHAPE_SEQUENTIAL = "sequential"  # 单线：一步接一步
SHAPE_PARALLEL = "parallel"      # 同级多 Agent 并发
SHAPE_JOIN = "join"              # 等所有前驱完成才跑


@dataclass(frozen=True, slots=True)
class StageShape:
    """一个 stage 的声明：谁跑、什么形状、跑在第几阶段。"""

    id: str
    role: str
    stage: int
    shape: str = SHAPE_SEQUENTIAL
    lane: int = 0
    #: 产出是否会被写进最终答案（False = 中间产物，只作为上游上下文）
    contributes_to_answer: bool = True


def _s(
    sid: str,
    role: str,
    stage: int,
    shape: str = SHAPE_SEQUENTIAL,
    lane: int = 0,
    *,
    final: bool = True,
) -> StageShape:
    return StageShape(
        id=sid,
        role=role,
        stage=stage,
        shape=shape,
        lane=lane,
        contributes_to_answer=final,
    )


@dataclass(frozen=True, slots=True)
class TopologySpec:
    """一个 profile 的完整拓扑声明。

    三个 builder 引用是**惰性字符串**（``module:attr``）：本模块被 graph 与
    planner 反向 import，直接引用会成环。
    """

    profile: str
    title: str
    stages: tuple[StageShape, ...]
    #: 静态图 builder：``(question) -> AgentGraph``
    graph_builder: str
    #: 模板 plan builder：``(question) -> Plan``
    plan_builder: str
    #: CrewAI stage-spec builder：``(llm=, tools=, question=) -> (graph, stages)``
    stage_builder: str | None = None
    #: 可并行工作项上限（仅 task_decomposition 之类的动态宽度用到）
    max_workers: int = 1
    #: 写-审-改一轮之后，最多允许几轮返修（B13 的界）
    max_revision_rounds: int = 1
    #: 该 profile 是否需要多 Agent 图 + 右侧面板
    multi_agent: bool = True
    #: 是否已在 durable engine 上验证过（灰度名单的语义来源）
    engine_supported: bool = True
    #: 是否允许在 run 结束时自动产出记忆提议（B11 的 profile 门）
    memory_auto_propose: bool = True
    #: 终轮（写答案那一轮）的 stage id，供 reviewer/verifier 对齐
    answer_stage: str = "writer"
    notes: str = ""


# --------------------------------------------------------------------------- #
# 声明。加一个 profile = 在这里加一条；其余地方（graph / planner / walker /
# orchestrator / 审计）都从这里读，不再各写一份清单。
# --------------------------------------------------------------------------- #
_TOPOLOGIES: tuple[TopologySpec, ...] = (
    TopologySpec(
        profile="deep_research",
        title="深度检索",
        stages=(
            _s("researcher", "researcher", 1, final=False),
            _s("analyst", "analyst", 2, SHAPE_JOIN, final=False),
            _s("writer", "writer", 3),
        ),
        graph_builder="app.agents.graph:build_deep_research_graph",
        plan_builder="app.agents.workflow.planner:build_deep_research_plan",
        stage_builder="app.agents.crews.research_crew:build_research_stages",
        notes="检索 → 交叉核对 → 成文。默认 profile，两条 walker 都已验证。",
    ),
    TopologySpec(
        profile="parallel_research",
        title="并行检索",
        stages=(
            _s("coordinator", "coordinator", 1, SHAPE_PARALLEL, 0, final=False),
            _s("coordinator", "coordinator", 1, SHAPE_PARALLEL, 1, final=False),
            _s("analyst", "analyst", 2, SHAPE_JOIN, final=False),
            _s("writer", "writer", 3),
        ),
        graph_builder="app.agents.graph:build_parallel_research_graph",
        plan_builder="app.agents.workflow.planner:build_parallel_research_plan",
        stage_builder=(
            "app.agents.crews.parallel_research:build_parallel_research_stages"
        ),
        notes="网络与知识库两路并发，analyst 汇合后成文。",
    ),
    TopologySpec(
        profile="debate",
        title="正反辩论",
        stages=(
            _s("side_a", "advocate", 1, SHAPE_PARALLEL, 0, final=False),
            _s("side_b", "advocate", 1, SHAPE_PARALLEL, 1, final=False),
            _s("judge", "judge", 2, SHAPE_JOIN),
        ),
        graph_builder="app.agents.graph:build_debate_graph",
        plan_builder="app.agents.workflow.planner:build_debate_plan",
        stage_builder="app.agents.crews.debate:build_debate_stages",
        answer_stage="judge",
        notes="双方各自论证，裁判在同一维度权衡后直接产出答案。",
    ),
    TopologySpec(
        profile="write_review",
        title="写-审-改",
        stages=(
            _s("drafter", "drafter", 1, final=False),
            _s("reviewer", "reviewer", 2, final=False),
            _s("finalizer", "finalizer", 3),
        ),
        graph_builder="app.agents.graph:build_write_review_graph",
        plan_builder="app.agents.workflow.planner:build_write_review_plan",
        stage_builder="app.agents.crews.write_review:build_write_review_stages",
        answer_stage="finalizer",
        notes=(
            "起草 → 审阅只诊断不改写 → 按意见定稿。审阅结论同时是 B13 "
            "引擎返修环的输入。"
        ),
    ),
    TopologySpec(
        profile="task_decomposition",
        title="任务拆解",
        stages=(
            _s("decomposer", "coordinator", 1, final=False),
            _s("worker", "worker", 2, SHAPE_PARALLEL, final=False),
            _s("integrator", "integrator", 3, SHAPE_JOIN),
        ),
        graph_builder="app.agents.graph:build_task_decomposition_graph",
        plan_builder="app.agents.workflow.planner:build_task_decomposition_plan",
        stage_builder=(
            "app.agents.crews.task_decomposition:build_task_decomposition_stages"
        ),
        max_workers=6,
        answer_stage="integrator",
        notes="拆解 → N 个并行工作项 → 全量整合。",
    ),
    TopologySpec(
        profile="general",
        title="单 Agent 助手",
        stages=(_s("assistant", "assistant", 1),),
        graph_builder="app.agents.graph:build_single_agent_graph",
        plan_builder="app.agents.workflow.planner:build_deep_research_plan",
        multi_agent=False,
        engine_supported=False,
        notes="不建多 Agent 图：无面板、无并发，只有一次工具调用循环。",
    ),
)

#: 路由/编排里出现的 profile 名到拓扑的映射。
TOPOLOGY_SPECS: dict[str, TopologySpec] = {t.profile: t for t in _TOPOLOGIES}

#: 未知 profile 的兜底 —— 与既有 ``build_graph_for_profile`` 的 else 分支一致。
DEFAULT_PROFILE = "deep_research"


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def declared_profiles() -> tuple[str, ...]:
    """本模块声明过的全部 profile（审计与 eval 的基线集合）。"""
    return tuple(TOPOLOGY_SPECS)


def topology_for(profile: str | None, *, strict: bool = False) -> TopologySpec | None:
    """按名字取拓扑。

    ``strict=False`` 时未知名字回落到 :data:`DEFAULT_PROFILE` —— 这是既有
    builder 的行为，保留它是为了「路由给了一个没听过的 profile 也不能崩」。
    ``strict=True`` 则返回 ``None``，供审计/eval 区分「真的声明了」与「兜底」。
    """
    key = (profile or "").strip()
    spec = TOPOLOGY_SPECS.get(key)
    if spec is not None:
        return spec
    if strict:
        return None
    return TOPOLOGY_SPECS[DEFAULT_PROFILE]


def is_known_profile(profile: str | None) -> bool:
    return (profile or "").strip() in TOPOLOGY_SPECS


def multi_agent_profiles() -> frozenset[str]:
    """walker 会走多 Agent 图的 profile 集合 —— ``_MULTI_AGENT_PROFILES`` 的
    唯一来源（以前是 crewai_runtime 里手写的一份）。"""
    return frozenset(
        t.profile for t in _TOPOLOGIES if t.multi_agent and t.stage_builder
    )


def resolve_builder(ref: str) -> Callable[..., Any]:
    """把 ``module:attr`` 解析成可调用对象。

    惰性 import 是有意的：拓扑表被 graph 与 planner 双向引用，模块级 import
    会成环；同时一个还没接线的 builder 不该在启动时拖垮整个 agents 包。
    """
    module_name, _, attr = ref.partition(":")
    if not attr:
        raise ValueError(f"builder 引用缺少属性名：{ref!r}")
    from importlib import import_module

    return getattr(import_module(module_name), attr)


def build_graph(spec: TopologySpec, question: str) -> Any:
    """按拓扑建静态图。``build_debate_graph`` 需要两侧论点，故单独处理。"""
    builder = resolve_builder(spec.graph_builder)
    if spec.profile == "debate":
        from app.agents.planning import extract_debate_sides

        sides = extract_debate_sides(question)
        return builder(
            sides.side_a if sides else "A", sides.side_b if sides else "B"
        )
    return builder(question)


def build_plan(spec: TopologySpec, question: str) -> Any:
    return resolve_builder(spec.plan_builder)(question)


def build_stage_specs(
    spec: TopologySpec, *, llm: Any, tools: Any, question: str
) -> Any:
    """CrewAI 路径的 ``(graph, stages)``。没声明 stage_builder 就是不支持。"""
    if not spec.stage_builder:
        raise LookupError(f"profile {spec.profile} 没有 CrewAI stage 构建器")
    return resolve_builder(spec.stage_builder)(
        llm=llm, tools=tools, question=question
    )


# --------------------------------------------------------------------------- #
# 求值（纯函数，可 eval）
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class TopologyReport:
    """一个 profile 的拓扑求值结果。字段全部可 JSON 化。"""

    profile: str
    declared: bool
    title: str
    shape: str
    stage_ids: list[str] = field(default_factory=list)
    stage_count: int = 0
    #: 按 ``stage`` 序号分组的宽度：``[1, 2, 1]`` = 一步、两步并行、一步
    lanes: list[int] = field(default_factory=list)
    answer_stage: str = ""
    multi_agent: bool = False
    engine_supported: bool = False
    max_workers: int = 1
    max_revision_rounds: int = 0
    memory_auto_propose: bool = False
    builders: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _lanes_of(stages: tuple[StageShape, ...]) -> list[int]:
    """每个 stage 序号上有多少个节点 —— 并行宽度一览。"""
    widths: dict[int, int] = {}
    for s in stages:
        widths[s.stage] = widths.get(s.stage, 0) + 1
    return [widths[k] for k in sorted(widths)]


def _shape_of(stages: tuple[StageShape, ...]) -> str:
    if not stages:
        return "empty"
    if any(s.shape == SHAPE_PARALLEL for s in stages):
        joined = any(s.shape == SHAPE_JOIN for s in stages)
        return "fan_out_join" if joined else "fan_out"
    return "chain"


def evaluate_topology(
    profile: str | None, *, question: str = "", plan: Any = None
) -> TopologyReport:
    """求值一个 profile 的拓扑。

    ``plan`` 给了就以 plan 的实际步骤覆盖静态声明的 stage 列表 —— LLM 规划器
    产出的 plan 可以比模板更宽，报告必须反映真实要跑的东西，否则「拓扑可
    评估」就只是把声明再念一遍。
    """
    key = (profile or "").strip()
    spec = topology_for(key, strict=True)
    if spec is None:
        spec = TOPOLOGY_SPECS[DEFAULT_PROFILE]
        warnings = [
            f"profile {key or '(空)'} 没有拓扑声明，按 {DEFAULT_PROFILE} 兜底执行"
        ]
        declared = False
    else:
        warnings = []
        declared = True

    stages = list(spec.stages)
    stage_ids = [s.id for s in stages]
    lanes = _lanes_of(spec.stages)

    if plan is not None:
        plan_ids = [str(getattr(s, "id", "")) for s in (getattr(plan, "steps", []) or [])]
        plan_ids = [pid for pid in plan_ids if pid]
        if plan_ids:
            stage_ids = plan_ids
            lanes = [1] * len(plan_ids)
            if len(set(plan_ids) & {s.id for s in spec.stages}) == 0:
                warnings.append(
                    "plan 的步骤与拓扑声明完全不相交：引擎将按动态 stage 执行，"
                    "静态图仅供展示"
                )

    report = TopologyReport(
        profile=key or DEFAULT_PROFILE,
        declared=declared,
        title=spec.title,
        shape=_shape_of(spec.stages),
        stage_ids=stage_ids,
        stage_count=len(stage_ids),
        lanes=lanes,
        answer_stage=spec.answer_stage,
        multi_agent=spec.multi_agent,
        engine_supported=spec.engine_supported,
        max_workers=spec.max_workers,
        max_revision_rounds=spec.max_revision_rounds,
        memory_auto_propose=spec.memory_auto_propose,
        builders={
            "graph": bool(spec.graph_builder),
            "plan": bool(spec.plan_builder),
            "crew_stages": bool(spec.stage_builder),
        },
        warnings=warnings,
    )
    return report


def topology_descriptor(settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """启动期 / ``/ready`` 用的拓扑自述。绝不抛异常。

    与 :func:`app.agents.sandbox.factory.runner_descriptor` 同一模式：运维
    必须能在不跑一次真实请求的情况下看到「这套部署到底会跑哪些拓扑」。
    """
    try:
        return {
            "declared_profiles": list(declared_profiles()),
            "default_profile": DEFAULT_PROFILE,
            "multi_agent_profiles": sorted(multi_agent_profiles()),
            "engine_capable_profiles": sorted(
                t.profile for t in _TOPOLOGIES if t.engine_supported
            ),
            "topologies": {
                t.profile: evaluate_topology(t.profile).as_dict()
                for t in _TOPOLOGIES
            },
        }
    except Exception as exc:  # pragma: no cover - 描述符绝不该打挂启动
        return {"error": str(exc)}
