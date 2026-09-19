"""名单式灰度：只有显式列出的 profile 走引擎。

安全默认：名单为空 → 没有任何 profile 走引擎，即使总开关为真。
这保证「开总开关」不会意外把所有 profile 都切过去。
"""

from __future__ import annotations

from app.agents.orchestrator import ChatOrchestrator, _engine_profiles
from app.core.config import get_settings


class _Sel:
    def __init__(self, profile: str) -> None:
        self.multi_agent_requested = True
        self.agent_profile = profile


def test_empty_roster_routes_nothing(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "AGENT_WORKFLOW_ENGINE", "1", raising=False)
    monkeypatch.setattr(s, "AGENT_WORKFLOW_ENGINE_PROFILES", "", raising=False)
    o = ChatOrchestrator()
    assert o._should_route_to_engine(_Sel("deep_research")) is False


def test_listed_profile_routes(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "AGENT_WORKFLOW_ENGINE", "1", raising=False)
    monkeypatch.setattr(
        s, "AGENT_WORKFLOW_ENGINE_PROFILES", "deep_research", raising=False
    )
    o = ChatOrchestrator()
    assert o._should_route_to_engine(_Sel("deep_research")) is True


def test_unlisted_profile_does_not_route(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "AGENT_WORKFLOW_ENGINE", "1", raising=False)
    monkeypatch.setattr(
        s, "AGENT_WORKFLOW_ENGINE_PROFILES", "deep_research", raising=False
    )
    o = ChatOrchestrator()
    assert o._should_route_to_engine(_Sel("debate")) is False


def test_master_switch_off_wins(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "AGENT_WORKFLOW_ENGINE", "", raising=False)
    monkeypatch.setattr(
        s, "AGENT_WORKFLOW_ENGINE_PROFILES", "deep_research", raising=False
    )
    o = ChatOrchestrator()
    assert o._should_route_to_engine(_Sel("deep_research")) is False


def test_roster_parsing_is_whitespace_tolerant(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(
        s,
        "AGENT_WORKFLOW_ENGINE_PROFILES",
        " deep_research , parallel_research ,, debate ",
        raising=False,
    )
    assert _engine_profiles(s) == frozenset(
        {"deep_research", "parallel_research", "debate"}
    )


def test_non_multi_agent_route_never_uses_engine(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "AGENT_WORKFLOW_ENGINE", "1", raising=False)
    monkeypatch.setattr(
        s, "AGENT_WORKFLOW_ENGINE_PROFILES", "deep_research", raising=False
    )

    class _Native:
        multi_agent_requested = False
        agent_profile = "deep_research"

    o = ChatOrchestrator()
    assert o._should_route_to_engine(_Native()) is False
