"""HermesProvider unit tests — no network.

Covers: session headers, local-tool stripping in the payload, and parsing of
interleaved ``hermes.tool.progress`` SSE events (meta deltas) alongside
standard OpenAI chunks.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.providers.base import ChatOptions
from app.providers.hermes import HermesProvider
from app.providers.registry import get_provider_for_config


def _provider(**kw) -> HermesProvider:
    defaults = dict(
        base_url="http://10.0.0.1:8642/v1",
        api_key="hk",
        model="hermes-agent",
        session_id="conv-1",
        session_key="user:u1",
    )
    defaults.update(kw)
    return HermesProvider(**defaults)


def test_session_headers_present():
    h = _provider()._headers()
    assert h["X-Hermes-Session-Id"] == "conv-1"
    assert h["X-Hermes-Session-Key"] == "user:u1"
    assert h["Authorization"] == "Bearer hk"


def test_session_headers_omitted_when_unset():
    h = _provider(session_id="", session_key="")._headers()
    assert "X-Hermes-Session-Id" not in h
    assert "X-Hermes-Session-Key" not in h


def test_local_tools_stripped_from_payload():
    tools = [{
        "type": "function",
        "function": {"name": "local_search", "description": "d", "parameters": {"type": "object", "properties": {}}},
    }]
    payload = HermesProvider._build_chat_payload(
        "hermes-agent",
        [{"role": "user", "content": "hi"}],
        ChatOptions(tools=tools, tool_choice="auto"),
        stream=True,
    )
    assert "tools" not in payload
    assert "tool_choice" not in payload
    assert payload["stream"] is True


def _sse_response(lines: list[str]) -> httpx.Response:
    body = "\n".join(lines)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=body))
    client = httpx.Client(transport=transport)
    req = client.build_request("POST", "http://10.0.0.1:8642/v1/chat/completions")
    return client.send(req, stream=True)


def _chunk(delta: dict[str, Any], finish: Any = None) -> str:
    return "data: " + json.dumps({
        "id": "c1", "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    })


@pytest.mark.asyncio
async def test_stream_with_tool_progress_meta():
    resp = _sse_response([
        _chunk({"role": "assistant"}),
        "event: hermes.tool.progress",
        "data: " + json.dumps({"tool": "web_search", "emoji": "🔎", "label": "AI news", "toolCallId": "call_1", "status": "running"}),
        _chunk({"content": "搜索中"}),
        "data: " + json.dumps({"tool": "web_search", "toolCallId": "call_1", "status": "completed"}),
        _chunk({"content": "结果"}, "stop"),
        "data: [DONE]",
    ])
    chunks = [c async for c in _provider()._iter_sse(resp)]
    resp.close()

    texts = "".join(c.content for c in chunks)
    assert texts == "搜索中结果"

    metas = [c.meta["hermes_tool"] for c in chunks if c.meta and "hermes_tool" in c.meta]
    assert len(metas) == 2
    assert metas[0]["status"] == "running"
    assert metas[0]["label"] == "AI news"
    assert metas[1]["status"] == "completed"

    assert chunks[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_stream_usage_snapshot_preserved():
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    resp = _sse_response([
        _chunk({"content": "x"}, "stop"),
        "data: " + json.dumps({"id": "c1", "object": "chat.completion.chunk", "choices": [], "usage": usage}),
        "data: [DONE]",
    ])
    chunks = [c async for c in _provider()._iter_sse(resp)]
    resp.close()
    assert chunks[-1].usage == usage


def _hermes_cfg():
    class _Cfg:
        provider = "hermes"
        api_base_url = "http://10.0.0.1:8642/v1"
        api_key_encrypted = ""
        model_name = "hermes-agent"
        output_token_parameter = "max_tokens"
        supports_tools = False
        supports_parallel_tools = False
        supports_vision = True
        supports_audio_input = False
        supports_audio_output = False
        supports_image_generation = False
        supports_structured_output = False
        supports_reasoning_effort = False
        max_context_tokens = 128000
        max_tokens = 8192
        temperature = 0.7
        top_p = 1.0

    return _Cfg()


def test_registry_builds_hermes_with_sessions():
    p = get_provider_for_config(_hermes_cfg(), session_id="conv-9", session_key="user:u9")  # type: ignore[arg-type]
    assert isinstance(p, HermesProvider)
    assert p.hermes_session_id == "conv-9"
    assert p.hermes_session_key == "user:u9"
    assert p.provider_name == "hermes"


def test_intent_router_hermes_mode():
    from app.agents.intent_router import decide_route

    route = decide_route("hermes", user_content="帮我搜点东西")
    assert route.mode == "hermes"
    assert route.enable_tools is False       # 本地工具不启用（服务端执行）
    assert route.use_multi_agent is False


def test_hermes_budget_overrides_raise_limits_when_authorized():
    """Hermes 模式注入的预算放宽必须在授权（allow_increase）下生效。"""
    from app.agents.policies.budget_policy import BudgetLimits

    base = BudgetLimits()
    raised = base.with_overrides(
        {
            "max_runtime_seconds": 900.0,
            "max_total_tokens": 1_000_000,
            "max_tool_output_chars": 200_000,
            "max_agent_steps": 64,
        },
        allow_increase=True,
    )
    assert raised.max_runtime_seconds == 900.0
    assert raised.max_total_tokens == 1_000_000
    assert raised.max_tool_output_chars == 200_000
    assert raised.max_agent_steps == 64
