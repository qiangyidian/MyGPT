"""ConfigStack: numeric-precedence config layering with per-key origin tracking."""
from __future__ import annotations

import pytest

from app.agents.config_layers import ConfigStack, Layer


# --------------------------------------------------------------------------- #
# merge semantics
# --------------------------------------------------------------------------- #
def test_higher_precedence_wins():
    stack = ConfigStack()
    stack.add(Layer("system", "sys", Layer.SYSTEM, {"model": "gpt-4"}))
    stack.add(Layer("user", "ui", Layer.USER, {"model": "gpt-3.5"}))
    assert stack.merge() == {"model": "gpt-3.5"}


def test_deep_merge_of_nested_dicts():
    stack = ConfigStack()
    stack.add(Layer("system", "sys", Layer.SYSTEM, {"a": {"b": 1, "c": 2}}))
    stack.add(Layer("project", "proj", Layer.PROJECT, {"a": {"c": 99, "d": 4}}))
    assert stack.merge() == {"a": {"b": 1, "c": 99, "d": 4}}


def test_lists_are_replaced_not_concatenated():
    stack = ConfigStack()
    stack.add(Layer("system", "sys", Layer.SYSTEM, {"tools": ["a", "b"]}))
    stack.add(Layer("project", "proj", Layer.PROJECT, {"tools": ["c"]}))
    assert stack.merge() == {"tools": ["c"]}


def test_merge_does_not_alias_layer_data():
    """Mutating the returned merge must not bleed back into the source layer."""
    src = Layer("system", "sys", Layer.SYSTEM, {"a": {"b": [1]}})
    stack = ConfigStack()
    stack.add(src)
    merged = stack.merge()
    merged["a"]["b"].append(2)
    assert src.data == {"a": {"b": [1]}}  # source untouched


# --------------------------------------------------------------------------- #
# origins
# --------------------------------------------------------------------------- #
def test_origins_point_to_winning_layer():
    stack = ConfigStack()
    stack.add(Layer("system", "sys", Layer.SYSTEM, {"model": "gpt-4", "temp": 0.0}))
    stack.add(Layer("project", "proj", Layer.PROJECT, {"model": "claude"}))
    origins = stack.origins()
    assert origins["model"] == "project"
    assert origins["temp"] == "system"


def test_origins_for_nested_keys_split_by_layer():
    stack = ConfigStack()
    stack.add(Layer("system", "sys", Layer.SYSTEM, {"a": {"b": 1}}))
    stack.add(Layer("user", "ui", Layer.USER, {"a": {"c": 2}}))
    assert stack.origins() == {"a.b": "system", "a.c": "user"}


def test_origins_drops_stale_subkeys_when_dict_replaced_by_scalar():
    stack = ConfigStack()
    stack.add(Layer("system", "sys", Layer.SYSTEM, {"a": {"b": 1, "c": 2}}))
    stack.add(Layer("session", "ovr", Layer.SESSION, {"a": 5}))
    assert stack.merge() == {"a": 5}
    origins = stack.origins()
    assert "a.b" not in origins
    assert origins == {"a": "session"}


# --------------------------------------------------------------------------- #
# fingerprint
# --------------------------------------------------------------------------- #
def test_fingerprint_is_sha256_hex_and_stable_for_same_data():
    stack_a = ConfigStack()
    stack_a.add(Layer("system", "sys", Layer.SYSTEM, {"model": "gpt-4", "nested": {"x": 1}}))
    fp_a = stack_a.fingerprint("system")

    # Same content, different dict key insertion order -> same fingerprint.
    stack_b = ConfigStack()
    stack_b.add(Layer("system", "sys", Layer.SYSTEM, {"nested": {"x": 1}, "model": "gpt-4"}))
    fp_b = stack_b.fingerprint("system")

    assert len(fp_a) == 64
    assert fp_a == fp_b


def test_fingerprint_differs_on_change():
    stack = ConfigStack()
    stack.add(Layer("system", "sys", Layer.SYSTEM, {"model": "gpt-4"}))
    before = stack.fingerprint("system")
    stack.layers[0].data["model"] = "gpt-5"
    after = stack.fingerprint("system")
    assert before != after


def test_fingerprint_unknown_layer_raises():
    stack = ConfigStack()
    stack.add(Layer("system", "sys", Layer.SYSTEM, {}))
    with pytest.raises(KeyError):
        stack.fingerprint("nope")


# --------------------------------------------------------------------------- #
# set_override
# --------------------------------------------------------------------------- #
def test_set_override_creates_session_layer_that_wins():
    stack = ConfigStack()
    stack.add(Layer("system", "sys", Layer.SYSTEM, {"model": "gpt-4"}))
    stack.add(Layer("user", "ui", Layer.USER, {"model": "gpt-3.5"}))

    stack.set_override("model", "claude")

    assert stack.merge() == {"model": "claude"}
    session_layers = [l for l in stack.layers if l.precedence == Layer.SESSION]
    assert len(session_layers) == 1
    assert session_layers[0].data == {"model": "claude"}
    assert session_layers[0].name == "session"
    assert stack.origins()["model"] == "session"


def test_set_override_writes_nested_path_into_existing_dict():
    stack = ConfigStack()
    stack.add(Layer("system", "sys", Layer.SYSTEM, {"a": {"b": 1}}))
    stack.set_override("a.b", 99)
    assert stack.merge() == {"a": {"b": 99}}
    assert stack.origins()["a.b"] == "session"


def test_set_override_reuses_existing_session_layer():
    stack = ConfigStack()
    stack.add(Layer("session", "existing", Layer.SESSION, {"x": 1}))
    stack.add(Layer("system", "sys", Layer.SYSTEM, {"model": "gpt-4"}))
    stack.set_override("model", "claude")
    session_layers = [l for l in stack.layers if l.precedence == Layer.SESSION]
    assert len(session_layers) == 1  # no duplicate created
    assert stack.merge() == {"x": 1, "model": "claude"}
