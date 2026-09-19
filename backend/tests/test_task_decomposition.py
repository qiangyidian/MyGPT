"""任务分解拓扑：decomposer → worker×N（并行）→ integrator（全量 join）。"""
from __future__ import annotations

from app.agents.graph import (
    build_task_decomposition_graph,
    graph_from_plan,
)
from app.agents.workflow.planner import build_task_decomposition_plan

_Q = "把竞品调研拆成可并行执行的工作项"


def test_plan_shape():
    plan = build_task_decomposition_plan(_Q, worker_count=3)
    ids = plan.step_ids
    assert ids[0] == "decomposer"
    assert ids[-1] == "integrator"
    assert ids[1:4] == ["worker-1", "worker-2", "worker-3"]

    # 所有 worker 只依赖 decomposer → 引擎的 ready 集是 3（真并行）。
    for wid in ("worker-1", "worker-2", "worker-3"):
        assert plan.get(wid).dependencies == ["decomposer"]
    # integrator 是 join：依赖全部 worker。
    assert plan.get("integrator").dependencies == [
        "worker-1", "worker-2", "worker-3"
    ]
    plan.validate()


def test_plan_respects_worker_count():
    plan = build_task_decomposition_plan(_Q, worker_count=5)
    assert len([s for s in plan.step_ids if s.startswith("worker-")]) == 5
    assert plan.get("integrator").dependencies == [f"worker-{i}" for i in range(1, 6)]


def test_graph_topology_matches_plan():
    plan = build_task_decomposition_plan(_Q, worker_count=3)
    got = graph_from_plan(plan)
    want = build_task_decomposition_graph(_Q, worker_count=3)

    assert [n.id for n in got.nodes] == [n.id for n in want.nodes]
    assert [n.stage for n in got.nodes] == [n.stage for n in want.nodes]
    assert [n.lane for n in got.nodes] == [n.lane for n in want.nodes]
    assert {(e.source, e.target) for e in got.edges} == {
        (e.source, e.target) for e in want.edges
    }


def test_graph_presentation_is_chinese_product_copy():
    graph = build_task_decomposition_graph(_Q, worker_count=2)
    decomposer = graph.node("decomposer")
    assert decomposer is not None
    assert decomposer.name  # 展示名非空
    # 面向用户的是中文产品文案，不是 plan 模板里的英文技术串。
    assert any("一" <= ch <= "鿿" for ch in decomposer.role)


def test_workers_share_one_stage_for_parallel_rendering():
    graph = build_task_decomposition_graph(_Q, worker_count=3)
    stages = {n.id: n.stage for n in graph.nodes}
    assert stages["decomposer"] == 0
    assert stages["worker-1"] == stages["worker-2"] == stages["worker-3"] == 1
    assert stages["integrator"] == 2
    # 同 stage 内的 lane 各不相同（面板并排渲染依赖此）。
    lanes = {n.id: n.lane for n in graph.nodes if n.stage == 1}
    assert sorted(lanes.values()) == [0, 1, 2]


def test_worker_count_is_clamped():
    """防止调用方传入 0 或负数导致空 worker 集。"""
    plan = build_task_decomposition_plan(_Q, worker_count=0)
    assert any(s.startswith("worker-") for s in plan.step_ids)
    plan2 = build_task_decomposition_plan(_Q, worker_count=-3)
    assert any(s.startswith("worker-") for s in plan2.step_ids)
