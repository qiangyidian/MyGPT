"""写-审-改拓扑：drafter → reviewer → finalizer（严格串行）。"""
from __future__ import annotations

from app.agents.graph import build_write_review_graph, graph_from_plan
from app.agents.workflow.planner import build_write_review_plan

_Q = "写一篇发布说明，说明这次 Agent 能力升级"


def test_plan_is_strictly_sequential():
    plan = build_write_review_plan(_Q)
    assert plan.step_ids == ["drafter", "reviewer", "finalizer"]
    assert plan.get("drafter").dependencies == []
    assert plan.get("reviewer").dependencies == ["drafter"]
    assert plan.get("finalizer").dependencies == ["reviewer"]
    plan.validate()


def test_graph_topology_matches_plan():
    plan = build_write_review_plan(_Q)
    got = graph_from_plan(plan)
    want = build_write_review_graph(_Q)
    assert [n.id for n in got.nodes] == [n.id for n in want.nodes]
    assert [n.stage for n in got.nodes] == [n.stage for n in want.nodes]
    assert {(e.source, e.target) for e in got.edges} == {
        (e.source, e.target) for e in want.edges
    }


def test_graph_uses_chinese_product_copy():
    graph = build_write_review_graph(_Q)
    for node in graph.nodes:
        assert node.role, f"{node.id} 缺 role"
        assert any("一" <= ch <= "鿿" for ch in node.role), (
            f"{node.id} 的 role 应当是中文产品文案"
        )


def test_stages_are_sequential():
    graph = build_write_review_graph(_Q)
    assert {n.id: n.stage for n in graph.nodes} == {
        "drafter": 0, "reviewer": 1, "finalizer": 2
    }
