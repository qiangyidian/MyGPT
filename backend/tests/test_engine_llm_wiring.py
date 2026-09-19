"""引擎路径按 flag 选择 planner / verifier。

默认关：不开 flag 时行为与现在完全一致（模板 plan + 规则 verifier）。
"""
from __future__ import annotations

from app.agents.orchestrator import ChatOrchestrator
from app.agents.workflow.llm_verifier import LLMVerifier
from app.agents.workflow.verifier import RuleBasedVerifier
from app.core.config import get_settings


def test_llm_planner_flag_default_off():
    assert get_settings().AGENT_LLM_PLANNER is False


def test_llm_verifier_flag_default_off():
    assert get_settings().AGENT_LLM_VERIFIER is False


def test_engine_builds_rule_verifier_by_default():
    """不开 flag 时必须用规则版 —— 这是已知行为。"""
    o = ChatOrchestrator()
    verifier = o._engine_verifier(provider=None, model_config=None, guard=None)
    assert isinstance(verifier, RuleBasedVerifier)


def test_engine_builds_llm_verifier_when_enabled(monkeypatch):
    monkeypatch.setattr(
        get_settings(), "AGENT_LLM_VERIFIER", True, raising=False
    )
    o = ChatOrchestrator()
    verifier = o._engine_verifier(
        provider=object(), model_config=None, guard=None
    )
    assert isinstance(verifier, LLMVerifier)


def test_engine_falls_back_to_rule_verifier_without_provider(monkeypatch):
    """flag 开着但拿不到 provider 时，必须回退规则版而不是崩。"""
    monkeypatch.setattr(
        get_settings(), "AGENT_LLM_VERIFIER", True, raising=False
    )
    o = ChatOrchestrator()
    verifier = o._engine_verifier(provider=None, model_config=None, guard=None)
    assert isinstance(verifier, RuleBasedVerifier)
