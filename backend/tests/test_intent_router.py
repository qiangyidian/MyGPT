"""Intent router: user-facing mode -> execution route mapping (pure unit tests)."""
from __future__ import annotations

from app.agents.intent_router import VALID_MODES, decide_route, filter_tool_names
from app.agents.schemas import ExecutionMode


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


def test_invalid_mode_falls_back_to_auto():
    r = decide_route("nonsense")
    assert r.mode == "auto"
    assert r.execution_mode == ExecutionMode.auto


def test_valid_modes_set():
    assert VALID_MODES == {
        "auto",
        "search",
        "deep_research",
        "create",
        "data_analysis",
        "debate",
    }


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
