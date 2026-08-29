"""AnthropicProvider unit tests — wire-format translation, no network.

Covers: system extraction, message conversion (text/image/tool_calls/tool
results), tool schema conversion, payload building (temperature XOR top_p,
stop_sequences, effort mapping), SSE stream parsing (text deltas, tool input
accumulation, finish_reason + usage), stop_reason mapping, and embeddings
refusal. HTTP is faked with a tiny httpx.MockTransport.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.providers.anthropic import (
    AnthropicProvider,
    _blocks_to_result,
    _convert_message,
    _convert_tool_choice,
    _convert_tools,
    _map_usage,
    _split_system,
)
from app.providers.base import ChatOptions, ProviderError


# ---------------------------------------------------------------- conversion
def test_split_system_extracts_and_folds():
    system, rest = _split_system([
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hi"},
        {"role": "system", "content": "Be terse."},
        {"role": "assistant", "content": "Hello!"},
    ])
    assert system == "You are helpful.\n\nBe terse."
    assert [m["role"] for m in rest] == ["user", "assistant"]


def test_convert_plain_user_message():
    assert _convert_message({"role": "user", "content": "hello"}) == {
        "role": "user", "content": "hello",
    }


def test_convert_tool_result_message():
    out = _convert_message({
        "role": "tool", "tool_call_id": "toolu_1", "content": "72°F",
    })
    assert out == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "72°F"}],
    }


def test_convert_assistant_tool_calls():
    out = _convert_message({
        "role": "assistant",
        "content": "Let me check.",
        "tool_calls": [{
            "id": "toolu_2",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
        }],
    })
    assert out["role"] == "assistant"
    blocks = out["content"]
    assert blocks[0] == {"type": "text", "text": "Let me check."}
    assert blocks[1]["type"] == "tool_use"
    assert blocks[1]["id"] == "toolu_2"
    assert blocks[1]["name"] == "get_weather"
    assert blocks[1]["input"] == {"city": "Paris"}


def test_convert_image_url_part_http():
    out = _convert_message({
        "role": "user",
        "content": [
            {"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
        ],
    })
    blocks = out["content"]
    assert blocks[1] == {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}


def test_convert_image_data_uri():
    out = _convert_message({
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    })
    assert out["content"][0]["source"] == {
        "type": "base64", "media_type": "image/png", "data": "AAAA",
    }


def test_empty_message_drops():
    assert _convert_message({"role": "user", "content": ""}) is None


# ------------------------------------------------------------------ tools
def test_convert_tools_openai_schema():
    tools = [{
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search the web",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }]
    out = _convert_tools(tools)
    assert out == [{
        "name": "search",
        "description": "Search the web",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }]


def test_convert_tool_choice_variants():
    assert _convert_tool_choice("auto") == {"type": "auto"}
    assert _convert_tool_choice("required") == {"type": "any"}
    assert _convert_tool_choice({"type": "function", "function": {"name": "search"}}) == {
        "type": "tool", "name": "search",
    }
    assert _convert_tool_choice("bogus") is None


# ------------------------------------------------------------------ payload
def _provider() -> AnthropicProvider:
    return AnthropicProvider(base_url="", api_key="sk-test", model="claude-sonnet-4-6")


def test_build_payload_shape():
    p = _provider()
    payload = p._build_payload(
        [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "hi"},
        ],
        ChatOptions(temperature=0.3, max_tokens=512, stop=["END"]),
        stream=False,
    )
    assert payload["model"] == "claude-sonnet-4-6"
    assert payload["max_tokens"] == 512
    assert payload["system"] == "SYS"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["temperature"] == 0.3
    assert payload["stop_sequences"] == ["END"]
    assert "stream" not in payload
    assert "top_p" not in payload  # temperature wins; never both


def test_build_payload_defaults_max_tokens():
    payload = _provider()._build_payload(
        [{"role": "user", "content": "x"}], ChatOptions(max_tokens=None), stream=True
    )
    assert payload["max_tokens"] == 4096
    assert payload["stream"] is True


def test_build_payload_effort_mapping():
    payload = _provider()._build_payload(
        [{"role": "user", "content": "x"}],
        ChatOptions(extra={"reasoning_effort": "high"}),
        stream=False,
    )
    assert payload["output_config"] == {"effort": "high"}


def test_build_payload_tools_and_choice():
    payload = _provider()._build_payload(
        [{"role": "user", "content": "weather?"}],
        ChatOptions(
            tools=[{"type": "function", "function": {
                "name": "w", "description": "d", "parameters": {"type": "object", "properties": {}},
            }}],
            tool_choice="auto",
        ),
        stream=False,
    )
    assert payload["tools"][0]["name"] == "w"
    assert payload["tool_choice"] == {"type": "auto"}


def test_messages_url_normalization():
    assert _provider()._messages_url() == "https://api.anthropic.com/v1/messages"
    p2 = AnthropicProvider(base_url="https://gw.example.com/v1", api_key="k", model="m")
    assert p2._messages_url() == "https://gw.example.com/v1/messages"


# ------------------------------------------------------------------ response
def test_blocks_to_result_text_and_tool_use():
    text, calls = _blocks_to_result([
        {"type": "text", "text": "Checking..."},
        {"type": "tool_use", "id": "t1", "name": "w", "input": {"a": 1}},
    ])
    assert text == "Checking..."
    assert len(calls) == 1
    assert calls[0].id == "t1"
    assert json.loads(calls[0].arguments) == {"a": 1}


def test_map_usage():
    u = _map_usage({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100})
    assert u["prompt_tokens"] == 10
    assert u["completion_tokens"] == 5
    assert u["total_tokens"] == 15
    assert u["cache_read_input_tokens"] == 100


# ------------------------------------------------------------------ streaming
def _sse_stream(events: list[dict[str, Any]]) -> httpx.Response:
    lines = []
    for evt in events:
        lines.append(f"data: {json.dumps(evt)}")
    body = "\n\n".join(lines) + "\n\n"
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=body))
    client = httpx.Client(transport=transport)
    req = client.build_request("POST", "https://api.anthropic.com/v1/messages")
    return client.send(req, stream=True)


@pytest.mark.asyncio
async def test_stream_text_deltas_and_finish():
    resp = _sse_stream([
        {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
    ])
    p = _provider()
    chunks = [c async for c in p._iter_sse(resp)]
    resp.close()
    texts = "".join(c.content for c in chunks)
    assert texts == "Hello"
    assert chunks[-1].finish_reason == "stop"
    # input_tokens=3 (message_start) merged with output_tokens=2 (message_delta)
    assert chunks[-1].usage == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


@pytest.mark.asyncio
async def test_stream_tool_use_accumulation():
    resp = _sse_stream([
        {"type": "message_start", "message": {}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_9", "name": "search"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"q": '}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '"x"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}},
        {"type": "message_stop"},
    ])
    p = _provider()
    chunks = [c async for c in p._iter_sse(resp)]
    resp.close()
    tool_chunks = [c for c in chunks if c.tool_calls]
    assert len(tool_chunks) == 1
    call = tool_chunks[0].tool_calls[0]
    assert call.id == "toolu_9"
    assert call.name == "search"
    assert json.loads(call.arguments) == {"q": "x"}
    assert tool_chunks[0].finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_stream_error_event_raises():
    resp = _sse_stream([
        {"type": "error", "error": {"type": "overloaded_error", "message": "overloaded"}},
    ])
    p = _provider()
    with pytest.raises(ProviderError, match="overloaded"):
        async for _ in p._iter_sse(resp):
            pass
    resp.close()


@pytest.mark.asyncio
async def test_embeddings_refuses():
    with pytest.raises(ProviderError, match="embeddings"):
        await _provider().embeddings(["x"])


# ------------------------------------------------------------------ registry
def test_registry_builds_anthropic_provider():
    from app.providers.registry import get_provider_for_config

    class _Cfg:
        provider = "anthropic"
        api_base_url = ""
        api_key_encrypted = ""
        model_name = "claude-sonnet-4-6"
        output_token_parameter = "max_tokens"
        # capabilities fields consumed by capabilities_from_config
        supports_tools = True
        supports_parallel_tools = False
        supports_vision = False
        supports_audio_input = False
        supports_audio_output = False
        supports_image_generation = False
        supports_structured_output = False
        supports_reasoning_effort = True
        max_context_tokens = 200000
        max_tokens = 8192
        temperature = 0.7
        top_p = 1.0

    provider = get_provider_for_config(_Cfg())  # type: ignore[arg-type]
    assert isinstance(provider, AnthropicProvider)
    assert provider.base_url == "https://api.anthropic.com"
