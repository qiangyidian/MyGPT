"""新 profile 的路由：只认模型显式点名，不靠关键词猜测。"""
from __future__ import annotations

from app.agents.intent_router import decide_route, decide_route_with_intent
from app.agents.schemas import IntentDecision


def _intent(route: str, *, confidence: float = 0.9, kind: str = "document"):
    return IntentDecision(
        route=route, deliverable_kind=kind, confidence=confidence,
        rationale="test", tool_hints=[],
    )


def test_keyword_router_never_picks_new_profiles():
    """关键词路径不得把普通请求误判成新拓扑 —— 这是误触发的源头。"""
    for text in (
        "帮我完成一个任务",
        "审阅一下这段文字",
        "把这个分解成几步",
        "写一份报告然后审阅",
    ):
        d = decide_route("auto", user_content=text)
        assert d.agent_profile not in ("task_decomposition", "write_review"), (
            f"{text!r} 被关键词路径误路由到 {d.agent_profile}"
        )


def test_intent_route_task_decomposition():
    d = decide_route_with_intent(
        "auto", user_content="把调研拆成并行工作项", intent=_intent("task_decomposition")
    )
    assert d.agent_profile == "task_decomposition"
    assert d.use_multi_agent is True
    assert d.execution_mode.value == "agent"


def test_intent_route_write_review():
    d = decide_route_with_intent(
        "auto", user_content="写一份发布说明", intent=_intent("write_review")
    )
    assert d.agent_profile == "write_review"
    assert d.use_multi_agent is True


def test_low_confidence_intent_falls_back_to_keyword_router():
    d = decide_route_with_intent(
        "auto", user_content="把调研拆成并行工作项",
        intent=_intent("task_decomposition", confidence=0.1),
    )
    assert d.agent_profile != "task_decomposition"


def test_code_deliverable_wins_over_new_profile():
    """代码请求仍走 native —— 新拓扑的 writer 会截断代码。"""
    d = decide_route_with_intent(
        "auto", user_content="写一个贪吃蛇游戏",
        intent=_intent("write_review", kind="code"),
    )
    assert d.use_multi_agent is False
    assert d.disable_web is True


def test_speed_mode_never_escalates():
    d = decide_route_with_intent(
        "speed", user_content="把调研拆成并行工作项",
        intent=_intent("task_decomposition"),
    )
    assert d.use_multi_agent is False
    assert d.mode == "speed"
