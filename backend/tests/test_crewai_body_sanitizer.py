"""Regression: CrewAI's null-content assistant tool-call message is coerced to
'' so Anthropic-strict OpenAI-compatible gateways (e.g. the GLM proxy) don't 422
a tool-calling agent on its second turn."""
import json

from app.agents.runtime.crewai_runtime import _coerce_chat_body


def _body(messages):
    return json.dumps({"model": "glm-5.2", "messages": messages}).encode("utf-8")


def test_coerces_null_assistant_content_to_empty_string():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "web_search", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "web_search", "content": "hits"},
    ]
    out = _coerce_chat_body(_body(msgs))
    assert out is not None
    payload = json.loads(out)
    asst = next(m for m in payload["messages"] if m["role"] == "assistant")
    assert asst["content"] == ""           # null -> ""
    assert asst["tool_calls"]              # tool_calls preserved


def test_leaves_valid_assistant_content_unchanged():
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert _coerce_chat_body(_body(msgs)) is None


def test_does_not_touch_non_assistant_null():
    msgs = [{"role": "tool", "tool_call_id": "c1", "name": "x", "content": None}]
    assert _coerce_chat_body(_body(msgs)) is None


def test_non_chat_body_unchanged():
    assert _coerce_chat_body(b'{"foo": "bar"}') is None
    assert _coerce_chat_body(b"not json") is None
