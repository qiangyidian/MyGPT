"""Intent router: user-facing mode -> execution route mapping (pure unit tests)."""
from __future__ import annotations

from app.agents.intent_router import (
    VALID_MODES,
    decide_route,
    decide_route_with_intent,
    filter_tool_names,
)
from app.agents.schemas import ExecutionMode, IntentDecision


def test_auto_is_native_simple_chat():
    r = decide_route("auto")
    assert r.execution_mode == ExecutionMode.auto
    assert r.enable_tools is False
    assert r.use_multi_agent is False


def test_search_enables_web_tools_native():
    r = decide_route("search")
    assert r.execution_mode == ExecutionMode.auto
    assert r.enable_tools is True
    assert r.use_multi_agent is False
    assert set(r.tool_allowlist) == {"web_search", "http_get"}


def test_deep_research_without_kb_uses_sequential_crew():
    r = decide_route("deep_research", has_knowledge_base=False)
    assert r.execution_mode == ExecutionMode.agent
    assert r.agent_profile == "deep_research"
    assert r.use_multi_agent is True


def test_deep_research_with_kb_uses_parallel_crew():
    r = decide_route("deep_research", has_knowledge_base=True)
    assert r.execution_mode == ExecutionMode.agent
    assert r.agent_profile == "parallel_research"
    assert r.use_multi_agent is True


def test_create_disables_web():
    r = decide_route("create")
    assert r.enable_tools is False
    assert r.disable_web is True


def test_data_analysis_enables_tools():
    r = decide_route("data_analysis")
    assert r.enable_tools is True
    assert r.use_multi_agent is False


def test_invalid_mode_falls_back_to_speed():
    # The UI picker default is 极速 (speed); an unknown mode falls back to it.
    r = decide_route("nonsense")
    assert r.mode == "speed"
    assert r.execution_mode == ExecutionMode.auto
    assert r.use_multi_agent is False


def test_valid_modes_set():
    # speed | expert | hermes are the exposed picker modes; the rest remain
    # valid for backward compatibility (legacy clients / internal escalation /
    # tests).
    assert {
        "speed", "expert", "hermes",
        "auto", "search", "deep_research", "create", "data_analysis", "debate",
    } == VALID_MODES


def test_speed_mode_is_native_no_multi_agent_but_can_search():
    r = decide_route("speed")
    # 极速：不多 Agent，但允许联网搜索（web_search / http_get）→ 能有「来源」。
    assert r.use_multi_agent is False
    assert r.execution_mode == ExecutionMode.auto
    assert r.enable_tools is True
    assert r.tool_allowlist == ["web_search", "http_get"]


def test_expert_mode_uses_multi_agent():
    r = decide_route("expert")
    assert r.use_multi_agent is True
    assert r.execution_mode == ExecutionMode.agent
    assert r.agent_profile == "deep_research"


def test_expert_mode_with_kb_uses_parallel_crew():
    r = decide_route("expert", has_knowledge_base=True)
    assert r.use_multi_agent is True
    assert r.agent_profile == "parallel_research"


def test_speed_ignores_intent_judgment():
    # 极速 always stays native even if an intent decision suggests multi-agent.
    from app.agents.schemas import IntentDecision

    intent = IntentDecision(
        route="deep_research", deliverable_kind="factual",
        tool_hints=["web_search"], confidence=0.9, rationale="x",
    )
    r = decide_route_with_intent("speed", user_content="research LLMs", intent=intent)
    assert r.use_multi_agent is False
    assert r.mode == "speed"


def test_filter_tool_names_allowlist_and_disable_web():
    names = ["web_search", "http_get", "file_analyze", "python_exec"]
    # search allowlist keeps only web tools
    search = decide_route("search")
    assert set(filter_tool_names(names, search)) == {"web_search", "http_get"}
    # create disables web but keeps the rest (allowlist is None there)
    create = decide_route("create")
    assert "web_search" not in filter_tool_names(names, create)
    assert "file_analyze" in filter_tool_names(names, create)


# ---- auto-mode intent-driven multi-agent escalation (less-conservative) ---- #
def test_auto_research_intent_escalates_to_multi_agent():
    r = decide_route("auto", user_content="请帮我深入调研一下大模型微调的主流方法与对比")
    assert r.execution_mode == ExecutionMode.agent
    assert r.use_multi_agent is True
    assert r.agent_profile == "deep_research"
    assert r.mode == "deep_research"
    assert r.requested_mode == "auto"


def test_auto_research_intent_with_kb_uses_parallel_crew():
    r = decide_route(
        "auto",
        user_content="请帮我深入调研一下大模型微调的主流方法与对比",
        has_knowledge_base=True,
    )
    assert r.use_multi_agent is True
    assert r.agent_profile == "parallel_research"


def test_auto_short_research_intent_stays_native():
    # Below the min-length threshold -> stays native (no crew for one-liners).
    r = decide_route("auto", user_content="分析下")
    assert r.use_multi_agent is False
    assert r.execution_mode == ExecutionMode.auto


def test_auto_plain_chat_stays_native():
    r = decide_route("auto", user_content="今天天气怎么样，出门要带伞吗")
    assert r.use_multi_agent is False
    assert r.execution_mode == ExecutionMode.auto


# ---- decide_route_with_intent: model-driven routing ----------------------- #
def _intent(route="native", kind="factual", confidence=0.9, hints=None):
    return IntentDecision(
        route=route, deliverable_kind=kind, confidence=confidence, tool_hints=hints or []
    )


def test_intent_code_routes_native_no_web():
    # A code request is forced native + web off, even if the model hinted research.
    r = decide_route_with_intent(
        "auto", user_content="写贪吃蛇", intent=_intent(route="deep_research", kind="code"),
    )
    assert r.execution_mode == ExecutionMode.auto
    assert r.use_multi_agent is False
    assert r.disable_web is True
    assert r.mode == "create"


def test_intent_research_routes_crew_parallel_with_kb():
    r = decide_route_with_intent(
        "auto", user_content="调研X", intent=_intent(route="deep_research"), has_knowledge_base=True,
    )
    assert r.execution_mode == ExecutionMode.agent
    assert r.agent_profile == "parallel_research"
    assert r.use_multi_agent is True


def test_intent_research_routes_sequential_without_kb():
    r = decide_route_with_intent(
        "auto", user_content="调研X", intent=_intent(route="deep_research"), has_knowledge_base=False,
    )
    assert r.agent_profile == "deep_research"
    assert r.use_multi_agent is True


def test_intent_debate_routes_debate_crew():
    r = decide_route_with_intent(
        "auto", user_content="A vs B", intent=_intent(route="debate"),
    )
    assert r.agent_profile == "debate"
    assert r.use_multi_agent is True
    assert r.disable_web is True


def test_intent_native_with_tool_hints_enables_those_tools():
    r = decide_route_with_intent(
        "auto", user_content="查一下X", intent=_intent(route="native", hints=["web_search"]),
    )
    assert r.enable_tools is True
    assert r.tool_allowlist == ["web_search"]


def test_intent_none_falls_back_to_keyword_router():
    r = decide_route_with_intent("auto", user_content="深入调研大模型微调", intent=None)
    # Falls back to decide_route -> research escalation for a research-flavored ask.
    assert r.use_multi_agent is True
    assert r.agent_profile == "deep_research"


def test_intent_low_confidence_falls_back():
    r = decide_route_with_intent(
        "auto", user_content="深入调研大模型微调", intent=_intent(route="native", confidence=0.2),
    )
    # Confidence below floor -> keyword router wins -> research crew, NOT native.
    assert r.use_multi_agent is True
    assert r.agent_profile == "deep_research"


def test_intent_at_confidence_floor_is_trusted():
    r = decide_route_with_intent(
        "auto", user_content="写贪吃蛇", intent=_intent(route="native", kind="code", confidence=0.5),
    )
    # >= floor -> trusted -> code => native.
    assert r.use_multi_agent is False
    assert r.disable_web is True
