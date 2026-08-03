"""Debate: a REAL multi-agent flow (advocate-a ‖ advocate-b → judge) + the
intent routing that escalates an explicit multi-agent / debate request to the
real multi-agent runtime instead of letting a single model role-play.

Covers: debate detection (planning), routing (intent_router), the debate graph
topology (advocates parallel, judge join; any A-vs-B pair, stable ids), and the
stage-spec structure the runtime walks.
"""
from __future__ import annotations

import pytest

from app.agents.graph import build_debate_graph, build_graph_for_profile
from app.agents.intent_router import decide_route
from app.agents.planning import (
    extract_debate_sides,
    looks_like_debate_request,
    looks_like_multi_agent_request,
)
from app.agents.schemas import ExecutionMode


# --------------------------------------------------------------------------- #
# Detection (planning)
# --------------------------------------------------------------------------- #
def test_extracts_vs_sides():
    s = extract_debate_sides("Python vs Go which is better")
    assert s is not None
    assert s.side_a.lower() == "python"
    assert s.side_b.lower() == "go"


def test_extracts_chinese_and_compare():
    s = extract_debate_sides("请比较 微服务 和 单体架构 的优劣")
    assert s is not None
    assert "微服务" in (s.side_a + s.side_b)
    assert "单体架构" in (s.side_a + s.side_b)


def test_debate_request_requires_multi_agent_signal():
    # explicit multi-agent + two candidates → debate
    assert looks_like_debate_request(
        "请使用多 Agent 比较 Python 和 Go，最后裁判总结"
    ) is True
    # plain comparison with NO agent/debate signal → NOT debate (no over-trigger)
    assert looks_like_debate_request("Python 和 Go 有什么区别") is False
    assert looks_like_debate_request("比较 Python 和 Go") is False


def test_multi_agent_request_without_two_sides():
    assert looks_like_multi_agent_request("请使用多个 Agent 一起研究这个问题") is True
    assert looks_like_multi_agent_request("今天天气怎么样") is False


# --------------------------------------------------------------------------- #
# Routing (intent_router)
# --------------------------------------------------------------------------- #
def test_explicit_debate_mode_routes_to_multi_agent():
    r = decide_route("debate", user_content="React vs Vue")
    assert r.execution_mode == ExecutionMode.agent
    assert r.agent_profile == "debate"
    assert r.use_multi_agent is True
    assert r.requested_mode == "debate"
    assert r.mode == "debate"


def test_auto_debate_intent_escalates():
    r = decide_route("auto", user_content="请使用多 Agent 比较 Python 和 Go，最后裁判总结")
    assert r.execution_mode == ExecutionMode.agent
    assert r.agent_profile == "debate"
    assert r.use_multi_agent is True
    # requested (auto) vs effective (debate) are distinct → observable escalation
    assert r.requested_mode == "auto"
    assert r.mode == "debate"


def test_auto_multi_agent_without_debate_goes_research():
    r = decide_route("auto", user_content="请使用多个 Agent 一起研究向量数据库的技术路线")
    assert r.use_multi_agent is True
    assert r.agent_profile in ("deep_research", "parallel_research")
    assert r.requested_mode == "auto"


def test_plain_compare_does_not_escalate():
    r = decide_route("auto", user_content="Python 和 Go 有什么区别")
    assert r.use_multi_agent is False
    assert r.mode == "auto"


def test_invalid_mode_falls_back_to_auto():
    r = decide_route("bogus", user_content="hi")
    assert r.mode == "auto"
    assert r.use_multi_agent is False


def test_deep_research_code_gen_reroute_preserved():
    # the streaming-task code-gen reroute must still work (not broken by debate).
    r = decide_route("deep_research", user_content="给我生成一个贪吃蛇的代码")
    assert r.mode == "create"
    assert r.requested_mode == "deep_research"


# --------------------------------------------------------------------------- #
# Graph topology (advocates parallel, judge join; stable ids; any pair)
# --------------------------------------------------------------------------- #
def test_debate_graph_structure():
    g = build_debate_graph("Python", "Go")
    assert [n.id for n in g.nodes] == ["advocate-a", "advocate-b", "judge"]
    by_id = {n.id: n for n in g.nodes}
    # advocates share a stage (parallel candidates)
    assert by_id["advocate-a"].stage == by_id["advocate-b"].stage
    # judge is a later stage (join)
    assert by_id["judge"].stage > by_id["advocate-a"].stage
    # judge depends on both advocates
    assert set(g.predecessors("judge")) == {"advocate-a", "advocate-b"}
    # display names carry the candidates; ids stay stable
    assert "Python" in by_id["advocate-a"].name
    assert "Go" in by_id["advocate-b"].name


def test_debate_graph_any_pair_not_hardcoded():
    g = build_debate_graph("PostgreSQL", "MySQL")
    names = " ".join(n.name for n in g.nodes)
    assert "PostgreSQL" in names and "MySQL" in names
    assert [n.id for n in g.nodes] == ["advocate-a", "advocate-b", "judge"]


def test_build_graph_for_profile_debate():
    g = build_graph_for_profile("debate", "React vs Vue")
    assert g.flow_name == "debate"
    assert any(n.id == "judge" for n in g.nodes)


# --------------------------------------------------------------------------- #
# Stage-spec structure (the runtime walks this: parallel advocates → judge join)
# --------------------------------------------------------------------------- #
def test_debate_stages_structure():
    try:
        from crewai import Agent  # noqa: F401
    except Exception:
        pytest.skip("crewai not installed")
    from app.agents.crews.debate import build_debate_stages

    graph, stages = build_debate_stages(llm=None, tools=[], question="Python vs Go")
    ids = [s.agent_id for s in stages]
    assert ids == ["advocate-a", "advocate-b", "judge"]
    advs = [s for s in stages if s.agent_id.startswith("advocate")]
    # both advocates at the same stage → run concurrently via asyncio.gather
    assert advs[0].stage == advs[1].stage
    judge = next(s for s in stages if s.agent_id == "judge")
    # judge joins on both advocates and runs at a later stage
    assert set(judge.depends_on) == {"advocate-a", "advocate-b"}
    assert judge.stage > advs[0].stage
    assert graph.flow_name == "debate"
