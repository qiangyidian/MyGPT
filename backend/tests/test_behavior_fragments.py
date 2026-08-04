"""Behavioral fragments: personality, mode behavior, remaining budget."""
from __future__ import annotations

from app.agents.behavior_fragments import (
    mode_behavior_fragment,
    personality_fragment,
    remaining_budget_fragment,
)


def test_mode_behavior_known_modes_have_directive():
    for mode in ("auto", "search", "deep_research", "create", "data_analysis", "debate"):
        f = mode_behavior_fragment(mode)
        assert f.tag == "mode_behavior"
        assert f.body.strip()
        assert "<mode_behavior>" in f.render()


def test_mode_behavior_unknown_mode_falls_back_to_auto():
    f = mode_behavior_fragment("nonsense")
    assert "默认模式" in f.body or "直接回答" in f.body


def test_personality_fragment_wraps_spec():
    f = personality_fragment("简洁、技术、少客套")
    assert "简洁、技术、少客套" in f.body
    assert "沟通风格" in f.body
    assert "<personality_spec>" in f.render()


def test_personality_fragment_empty_drops_out():
    assert personality_fragment("   ").render() == ""
    assert personality_fragment("").body.strip() == ""


def test_remaining_budget_mentions_given_fields_only():
    f = remaining_budget_fragment(remaining_tokens=1200, remaining_steps=4)
    assert "1200 token" in f.body and "4 步" in f.body
    only_tokens = remaining_budget_fragment(remaining_tokens=500)
    assert "500 token" in only_tokens.body and "步" not in only_tokens.body


def test_remaining_budget_empty_when_nothing_given():
    # No args at all -> nothing to say -> dropped.
    assert remaining_budget_fragment().render() == ""
    # 0 is a real (exhausted) budget -> surfaced, not dropped.
    zero = remaining_budget_fragment(remaining_tokens=0)
    assert "0 token" in zero.body
