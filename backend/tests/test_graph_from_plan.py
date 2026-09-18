"""graph_from_plan：plan 是拓扑真相，静态 builder 提供展示层。

核心保证：对既有三个 profile，graph_from_plan(build_*_plan(q)) 必须与
build_*_graph(q) 完全一致 —— 节点 id、依赖、stage 分组、lane、以及
面向用户的中文文案。任何一项不一致都会在子项目 2 开启引擎路由时
变成面板上的可见回归。
"""
from __future__ import annotations

from app.agents.graph import (
    build_deep_research_graph,
    build_parallel_research_graph,
    graph_from_plan,
)
from app.agents.workflow.planner import (
    build_deep_research_plan,
    build_parallel_research_plan,
)
from app.agents.workflow.schemas import Plan, Step

_Q = "对比 PostgreSQL 与 MySQL 的适用场景"


def _edge_pairs(graph) -> set[tuple[str, str]]:
    return {(e.source, e.target) for e in graph.edges}


def test_deep_research_topology_matches_static_graph():
    plan = build_deep_research_plan(_Q)
    got = graph_from_plan(plan)
    want = build_deep_research_graph(_Q)

    assert [n.id for n in got.nodes] == [n.id for n in want.nodes]
    assert _edge_pairs(got) == _edge_pairs(want)
    assert [n.stage for n in got.nodes] == [n.stage for n in want.nodes]
    assert [n.lane for n in got.nodes] == [n.lane for n in want.nodes]


def test_deep_research_presentation_matches_static_graph():
    got = graph_from_plan(build_deep_research_plan(_Q))
    want = build_deep_research_graph(_Q)
    for g, w in zip(got.nodes, want.nodes):
        assert g.name == w.name
        assert g.role == w.role
        assert g.task_title == w.task_title
        assert g.task_summary == w.task_summary


def test_parallel_research_topology_and_presentation_match():
    plan = build_parallel_research_plan(_Q)
    got = graph_from_plan(plan)
    want = build_parallel_research_graph(_Q)

    assert [n.id for n in got.nodes] == [n.id for n in want.nodes]
    assert _edge_pairs(got) == _edge_pairs(want)
    assert [n.stage for n in got.nodes] == [n.stage for n in want.nodes]
    assert [n.lane for n in got.nodes] == [n.lane for n in want.nodes]
    for g, w in zip(got.nodes, want.nodes):
        assert (g.name, g.role, g.task_title, g.task_summary) == (
            w.name, w.role, w.task_title, w.task_summary,
        )


def test_debate_stages_carry_dynamic_side_names():
    from app.agents.workflow.planner import build_debate_plan

    plan = build_debate_plan(_Q)
    got = graph_from_plan(plan)
    nodes = {n.id: n for n in got.nodes}
    assert nodes["advocate-a"].stage == 0
    assert nodes["advocate-b"].stage == 0
    assert nodes["judge"].stage == 1
    # advocate-a 先于 advocate-b 声明 -> lane 0 / 1（面板并排渲染依赖此序）。
    assert nodes["advocate-a"].lane == 0
    assert nodes["advocate-b"].lane == 1


def test_unknown_profile_falls_back_to_plan_derived_copy():
    """新 profile（无静态 builder）不得误用 deep_research 的文案。"""
    plan = Plan(
        version=1, goal="g", profile="brand_new",
        steps=[
            Step(id="alpha", role="scout", name="Scout",
                 task_description="The alpha task.", dependencies=[]),
            Step(id="beta", role="editor", name="Editor",
                 task_description="The beta task.", dependencies=["alpha"]),
        ],
    )
    got = graph_from_plan(plan)
    assert [n.id for n in got.nodes] == ["alpha", "beta"]
    assert [n.name for n in got.nodes] == ["Scout", "Editor"]
    assert got.nodes[0].stage == 0 and got.nodes[1].stage == 1
    assert got.nodes[0].task_summary == "The alpha task."
    assert _edge_pairs(got) == {("alpha", "beta")}


def test_unknown_profile_never_borrows_deep_research_copy():
    plan = Plan(
        version=1, goal="g", profile="brand_new",
        steps=[Step(id="solo", name="Solo", task_description="only")],
    )
    nodes = {n.id: n for n in graph_from_plan(plan).nodes}
    assert "solo" in nodes
    assert "researcher" not in nodes
