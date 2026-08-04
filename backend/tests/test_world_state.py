"""World-state diffing + fragment markers (Codex incremental-context pattern)."""
from __future__ import annotations

from app.agents.context_fragments import ContextFragment
from app.agents.world_state import WorldStateDiffer, differ_for, drop_differ


def _frag(name: str, tag: str, body: str) -> ContextFragment:
    return ContextFragment(name=name, tag=tag, body=body)


def test_markers_and_contains_tag():
    f = _frag("env", "environment", "kb=docs")
    assert f.markers() == ("<environment>", "</environment>")
    assert ContextFragment.contains_tag("foo <environment>..", "environment") is True
    assert ContextFragment.contains_tag("no marker here", "environment") is False


def test_diff_emits_only_new_or_changed():
    d = WorldStateDiffer()
    a = _frag("env", "environment", "kb=docs")
    b = _frag("mode", "current_mode", "auto")
    # First call: both are new -> both emitted.
    assert {f.name for f in d.diff([a, b])} == {"env", "mode"}
    # Second call, unchanged -> nothing emitted.
    assert d.diff([a, b]) == []


def test_diff_emits_changed_fragment_only():
    d = WorldStateDiffer()
    a = _frag("env", "environment", "kb=docs")
    d.diff([a])
    # env changes, mode is new.
    a2 = _frag("env", "environment", "kb=docs2")
    m = _frag("mode", "current_mode", "auto")
    changed = {f.name for f in d.diff([a2, m])}
    assert changed == {"env", "mode"}


def test_diff_drops_disappeared_and_reemits_on_return():
    d = WorldStateDiffer()
    a = _frag("env", "environment", "kb=docs")
    d.diff([a])
    # env absent this round -> nothing emitted, snapshot drops it.
    assert d.diff([]) == []
    # env returns -> re-emitted (not silently skipped).
    assert {f.name for f in d.diff([a])} == {"env"}


def test_diff_ignores_empty_body():
    d = WorldStateDiffer()
    empty = _frag("notes", "notes", "   ")
    assert d.diff([empty]) == []
    # Now it becomes non-empty -> emitted.
    full = _frag("notes", "notes", "hello")
    assert {f.name for f in d.diff([full])} == {"notes"}


def test_per_conversation_cache():
    drop_differ("conv-1")
    d1 = differ_for("conv-1")
    d2 = differ_for("conv-1")
    assert d1 is d2  # same baseline per conversation
    d3 = differ_for("conv-2")
    assert d3 is not d1  # different conversations isolated
    drop_differ("conv-1")
    assert differ_for("conv-1") is not d1  # fresh after drop
