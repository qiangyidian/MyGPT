"""tool_compose: restricted declarative tool-composition DSL (safe code-mode)."""
from __future__ import annotations

import pytest

from app.agents.tool_compose import ToolComposeError, execute_program, parse_program


def _fake_caller(table: dict[str, object]):
    def _call(tool: str, args: dict):
        if tool not in table:
            raise RuntimeError(f"unknown tool {tool}")
        return table[tool](**args) if callable(table[tool]) else table[tool]
    return _call


def test_execute_interpolates_index_ref():
    program = {"ops": [
        {"tool": "lookup", "args": {"q": "x"}, "as": "first"},
        {"tool": "fetch", "args": {"url": "${0.url}"}},
    ]}
    table = {
        "lookup": lambda q: {"url": "http://ex/" + q},
        "fetch": lambda url: {"got": url},
    }
    res = execute_program(program, _fake_caller(table))
    assert res.results[0] == {"url": "http://ex/x"}
    assert res.results[1] == {"got": "http://ex/x"}


def test_execute_named_ref():
    program = {"ops": [
        {"tool": "gen", "args": {}, "as": "tok"},
        {"tool": "use", "args": {"t": "${tok.value}"}},
    ]}
    res = execute_program(program, _fake_caller({"gen": lambda: {"value": "abc"}, "use": lambda t: {"ok": t}}))
    assert res.results[1] == {"ok": "abc"}


def test_substring_interpolation():
    program = {"ops": [
        {"tool": "name", "args": {}},
        {"tool": "greet", "args": {"msg": "hi ${0.n}!"}},
    ]}
    res = execute_program(program, _fake_caller({"name": lambda: {"n": "kim"}, "greet": lambda msg: msg}))
    assert res.results[1] == "hi kim!"


def test_op_cap_rejected():
    program = {"ops": [{"tool": "x", "args": {}} for _ in range(25)]}
    with pytest.raises(ToolComposeError):
        parse_program(program)


def test_unknown_reference_raises():
    program = {"ops": [{"tool": "a", "args": {"x": "${9.no}"}}]}
    with pytest.raises(ToolComposeError):
        execute_program(program, _fake_caller({"a": lambda x: x}))


def test_tool_failure_captured_as_error():
    program = {"ops": [{"tool": "boom", "args": {}}]}
    res = execute_program(program, _fake_caller({"boom": lambda: (_ for _ in ()).throw(RuntimeError("nope"))}))
    assert res.results[0] == {"error": "nope"}
