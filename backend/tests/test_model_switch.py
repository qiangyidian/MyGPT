"""model_switch: comp_hash gating + downshift detection."""
from app.agents.model_switch import (
    comp_hash,
    is_downshift,
    recompact_on_downshift,
    should_recompact_on_switch,
)
from app.agents.context_manager import ContextManager


def test_comp_hash_stable_and_sensitive():
    a = comp_hash(provider="openai", model_name="gpt-x", context_window_tokens=128000)
    assert a == comp_hash(provider="openai", model_name="gpt-x", context_window_tokens=128000)
    # Different window -> different hash.
    assert a != comp_hash(provider="openai", model_name="gpt-x", context_window_tokens=200000)
    # Different model -> different hash.
    assert a != comp_hash(provider="openai", model_name="gpt-y", context_window_tokens=128000)


def test_should_recompact_on_hash_change():
    h1 = comp_hash(provider="openai", model_name="a", context_window_tokens=128000)
    h2 = comp_hash(provider="openai", model_name="b", context_window_tokens=128000)
    assert should_recompact_on_switch(h1, h2) is True
    assert should_recompact_on_switch(h1, h1) is False
    assert should_recompact_on_switch(None, h1) is False


def test_downshift_detection():
    # New window smaller and active tokens exceed it -> downshift.
    assert is_downshift(previous_window_tokens=200000, current_window_tokens=128000, active_tokens=150000) is True
    # Smaller window but not yet over the new limit -> not a downshift yet.
    assert is_downshift(previous_window_tokens=200000, current_window_tokens=128000, active_tokens=1000) is False
    # Larger window -> never a downshift.
    assert is_downshift(previous_window_tokens=128000, current_window_tokens=200000, active_tokens=999999) is False


def test_recompact_on_downshift_triggers_context_manager():
    """On a downshift, recompact_on_downshift drives the ONE ContextManager to
    produce a compacted transcript that fits the new (smaller) window."""
    mgr = ContextManager(summarize_fn=lambda older: f"SUMMARY of {len(older)}")
    active = [{"role": "system", "content": "SYS"}]
    for i in range(10):
        active.append({"role": "user", "content": f"ask {i} " + "a" * 3000})
        active.append({"role": "assistant", "content": f"ans {i} " + "b" * 3000})

    directive = recompact_on_downshift(
        mgr,
        previous_window_tokens=200_000,
        current_window_tokens=8_000,
        active_messages=active,
    )
    assert directive.must_recompact is True
    assert len(directive.compacted_messages) < len(active)


def test_recompact_on_downshift_noop_on_upshift():
    mgr = ContextManager(summarize_fn=lambda older: "S")
    directive = recompact_on_downshift(
        mgr,
        previous_window_tokens=8_000,
        current_window_tokens=200_000,
        active_messages=[{"role": "system", "content": "SYS"}],
    )
    assert directive.must_recompact is False
