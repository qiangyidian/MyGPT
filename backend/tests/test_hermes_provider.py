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

from app.providers.base import ChatDelta, ChatOptions
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


# --------------------------------------------------------------------------
# Runs API transport (2026-08 upgrade)
# --------------------------------------------------------------------------

_CAPS_RUNS_OK = {
    "object": "hermes.api_server.capabilities",
    "features": {
        "run_submission": True,
        "run_events_sse": True,
        "run_stop": True,
    },
}

_CAPS_RUNS_OFF = {
    "object": "hermes.api_server.capabilities",
    "features": {"run_submission": False},
}


def _run_event(event: str, data: dict[str, Any]) -> str:
    return "data: " + json.dumps({"event": event, "data": data})


@pytest.mark.asyncio
async def test_runs_probe_true_when_all_features_present():
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json=_CAPS_RUNS_OK)

    p = _provider()
    p._transport = httpx.MockTransport(handler)
    # Patch the client factory used inside _probe_runs_support by binding the
    # mock transport onto a subclass-level factory hook: simplest is monkeypatching
    # httpx.AsyncClient kwargs via the provider attribute.
    import app.providers.hermes as hermes_mod

    orig_client = hermes_mod.httpx.AsyncClient

    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(**kw)

    hermes_mod.httpx.AsyncClient = _MockAsyncClient
    try:
        assert await p._probe_runs_support() is True
        # Cached: second probe makes no HTTP call.
        n = len(calls)
        assert await p._probe_runs_support() is True
        assert len(calls) == n
    finally:
        hermes_mod.httpx.AsyncClient = orig_client


@pytest.mark.asyncio
async def test_runs_probe_false_without_features():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_CAPS_RUNS_OFF)

    import app.providers.hermes as hermes_mod

    orig_client = hermes_mod.httpx.AsyncClient

    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(**kw)

    hermes_mod.httpx.AsyncClient = _MockAsyncClient
    try:
        assert await _provider()._probe_runs_support() is False
    finally:
        hermes_mod.httpx.AsyncClient = orig_client


@pytest.mark.asyncio
async def test_runs_stream_maps_events_to_chat_deltas():
    """Runs SSE → ChatDelta 契约：token / 工具 / 子代理 / 终态 / usage。"""
    sse_body = "\n".join([
        _run_event("run.started", {"run_id": "run_1"}),
        _run_event("assistant.delta", {"delta": "正在"}),
        _run_event("assistant.delta", {"delta": "搜索"}),
        _run_event("tool.started", {"tool": "web_search", "toolCallId": "call_1", "label": "AI news", "emoji": "🔎"}),
        _run_event("tool.completed", {"tool": "web_search", "toolCallId": "call_1", "status": "completed"}),
        _run_event("subagent.start", {"child_session_id": "child_1", "label": "分析数据"}),
        _run_event("subagent.complete", {"child_session_id": "child_1", "status": "completed", "summary": "分析完成", "duration": 10.5}),
        _run_event("assistant.delta", {"delta": "结果"}),
        _run_event("run.completed", {"usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}),
    ])
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text=sse_body, headers={"content-type": "text/event-stream"})
    )
    client = httpx.Client(transport=transport)
    req = client.build_request("GET", "http://10.0.0.1:8642/v1/runs/run_1/events")
    resp = client.send(req, stream=True)

    chunks = [c async for c in _provider()._iter_run_events(resp)]
    resp.close()

    assert "".join(c.content for c in chunks) == "正在搜索结果"

    tools = [c.meta["hermes_tool"] for c in chunks if c.meta and "hermes_tool" in c.meta]
    assert len(tools) == 2
    assert tools[0]["status"] == "running"
    assert tools[0]["label"] == "AI news"
    assert tools[0]["emoji"] == "🔎"
    assert tools[1]["status"] == "completed"

    subs = [c.meta["hermes_subagent"] for c in chunks if c.meta and "hermes_subagent" in c.meta]
    assert len(subs) == 2
    assert subs[0]["subagentId"] == "child_1"
    assert subs[0]["status"] == "running"
    assert subs[1]["status"] == "completed"
    assert subs[1]["duration"] == 10.5

    assert chunks[-1].finish_reason == "stop"
    assert chunks[-1].usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


@pytest.mark.asyncio
async def test_runs_stream_failed_maps_to_error_finish():
    sse_body = "\n".join([
        _run_event("run.failed", {"error": "boom"}),
    ])
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=sse_body))
    client = httpx.Client(transport=transport)
    req = client.build_request("GET", "http://x/v1/runs/r/events")
    resp = client.send(req, stream=True)
    chunks = [c async for c in _provider()._iter_run_events(resp)]
    resp.close()
    assert chunks[-1].finish_reason == "error"


@pytest.mark.asyncio
async def test_runs_stream_without_terminal_synthesizes_stop():
    sse_body = _run_event("assistant.delta", {"delta": "半途"})
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=sse_body))
    client = httpx.Client(transport=transport)
    req = client.build_request("GET", "http://x/v1/runs/r/events")
    resp = client.send(req, stream=True)
    chunks = [c async for c in _provider()._iter_run_events(resp)]
    resp.close()
    assert chunks[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_stream_via_runs_submits_then_subscribes():
    """_stream_via_runs：POST /runs 取 run_id → 订阅 events，携带会话头。"""
    seen: dict[str, httpx.Request] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen[req.method + " " + req.url.path] = req
        if req.method == "POST" and req.url.path.endswith("/runs"):
            return httpx.Response(200, json={"run_id": "run_42", "status": "started"})
        # events subscription
        body = "\n".join([
            _run_event("assistant.delta", {"delta": "ok"}),
            _run_event("run.completed", {}),
        ])
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    import app.providers.hermes as hermes_mod

    orig_client = hermes_mod.httpx.AsyncClient

    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(**kw)

    hermes_mod.httpx.AsyncClient = _MockAsyncClient
    try:
        p = _provider()
        chunks = [
            c async for c in p._stream_via_runs(
                [
                    {"role": "system", "content": "你是助手"},
                    {"role": "user", "content": "搜一下"},
                    {"role": "assistant", "content": "之前回答"},
                    {"role": "user", "content": "再搜最新"},
                ],
                None,
            )
        ]
        assert p._active_run_id is None  # cleared in finally
        assert "".join(c.content for c in chunks) == "ok"
        assert chunks[-1].finish_reason == "stop"

        submit = seen["POST /v1/runs"]
        body = json.loads(submit.content)
        assert body["input"] == "再搜最新"  # LAST user message only
        assert body["session_id"] == "conv-1"
        assert body["instructions"] == "你是助手"
        # Session memory headers ride the submit too.
        assert submit.headers["X-Hermes-Session-Id"] == "conv-1"
        assert submit.headers["X-Hermes-Session-Key"] == "user:u1"
        assert "GET /v1/runs/run_42/events" in seen
    finally:
        hermes_mod.httpx.AsyncClient = orig_client


@pytest.mark.asyncio
async def test_stream_chat_falls_back_when_runs_unsupported():
    """capabilities 不支持 → 走继承的 chat/completions SSE 路径。"""
    import app.providers.hermes as hermes_mod

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/capabilities"):
            return httpx.Response(200, json=_CAPS_RUNS_OFF)
        # chat/completions fallback stream
        assert req.url.path.endswith("/chat/completions")
        body = "\n".join([
            _chunk({"content": "回退"}),
            _chunk({}, "stop"),
            "data: [DONE]",
        ])
        return httpx.Response(200, text=body)

    orig_client = hermes_mod.httpx.AsyncClient

    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(**kw)

    hermes_mod.httpx.AsyncClient = _MockAsyncClient
    try:
        p = _provider()
        chunks = [
            c
            async for c in p.stream_chat(
                [{"role": "user", "content": "hi"}], None
            )
        ]
        assert "".join(c.content for c in chunks) == "回退"
        assert chunks[-1].finish_reason == "stop"
    finally:
        hermes_mod.httpx.AsyncClient = orig_client


@pytest.mark.asyncio
async def test_stream_chat_prefers_runs_when_supported():
    import app.providers.hermes as hermes_mod

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/capabilities"):
            return httpx.Response(200, json=_CAPS_RUNS_OK)
        if req.method == "POST" and req.url.path.endswith("/runs"):
            return httpx.Response(200, json={"run_id": "run_x"})
        return httpx.Response(
            200,
            text="\n".join([
                _run_event("assistant.delta", {"delta": "走runs"}),
                _run_event("run.completed", {}),
            ]),
            headers={"content-type": "text/event-stream"},
        )

    orig_client = hermes_mod.httpx.AsyncClient

    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(**kw)

    hermes_mod.httpx.AsyncClient = _MockAsyncClient
    try:
        p = _provider()
        chunks = [
            c
            async for c in p.stream_chat(
                [{"role": "user", "content": "hi"}], None
            )
        ]
        assert "".join(c.content for c in chunks) == "走runs"
    finally:
        hermes_mod.httpx.AsyncClient = orig_client


@pytest.mark.asyncio
async def test_stop_run_posts_stop_and_is_noop_when_idle():
    posts: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        posts.append(req.method + " " + req.url.path)
        return httpx.Response(200, json={"status": "stopping"})

    import app.providers.hermes as hermes_mod

    orig_client = hermes_mod.httpx.AsyncClient

    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(**kw)

    hermes_mod.httpx.AsyncClient = _MockAsyncClient
    try:
        p = _provider()
        # Idle → no-op, no HTTP.
        await p.stop_run()
        assert posts == []
        # Active run → POST /runs/{id}/stop.
        p._active_run_id = "run_9"
        await p.stop_run()
        assert posts == ["POST /v1/runs/run_9/stop"]
        # Consumed → second call is a no-op.
        await p.stop_run()
        assert len(posts) == 1
    finally:
        hermes_mod.httpx.AsyncClient = orig_client


# --------------------------------------------------------------------------
# File delivery (2026-08: chat file cards)
# --------------------------------------------------------------------------

from pathlib import Path

from app.providers.hermes import (
    _decode_b64_payload,
    _guess_media_type,
    extract_deliverable_paths,
)


def test_extract_deliverable_paths_filters_and_dedupes():
    text = "\n".join([
        "已生成 /root/report.pptx（64 KB）",
        "PDF 版： /root/report.pdf 重复一次 /root/report.pptx",
        "配置在 /etc/nginx/nginx.conf 不应交付",
        "无扩展名 /root/notes 忽略",
        "相对路径 ./local.txt 忽略",
        "Windows 路径 D:\\Reports\\Q3.xlsx 应识别",
        "危险类型 /tmp/evil.exe 忽略",
    ])
    paths = extract_deliverable_paths(text)
    assert paths == [
        "/root/report.pptx",
        "/root/report.pdf",
        "D:\\Reports\\Q3.xlsx",
    ]


def test_extract_deliverable_paths_empty_text():
    assert extract_deliverable_paths("") == []
    assert extract_deliverable_paths(None) == []  # type: ignore[arg-type]


def test_guess_media_type_common_extensions():
    assert _guess_media_type("/a/b/c.pptx").endswith("presentationml.presentation")
    assert _guess_media_type("/a/b/c.pdf") == "application/pdf"
    assert _guess_media_type("/a/b/c.png") == "image/png"
    assert _guess_media_type("/a/b/c.unknown") == "application/octet-stream"


def test_decode_b64_payload_roundtrip():
    import base64 as _b64

    payload = "<B64>" + _b64.b64encode(b"file-bytes-here").decode() + "</B64>"
    assert _decode_b64_payload(payload, "/x.txt") == b"file-bytes-here"

    # Whitespace inside the payload is tolerated (agents wrap long lines).
    spaced = "<B64>\n" + "\n".join(_b64.b64encode(b"x" * 64).decode()) + "\n</B64>"
    assert _decode_b64_payload(spaced, "/x.bin") == b"x" * 64


def test_decode_b64_payload_error_shapes():
    import pytest as _pytest

    # Explicit agent-side error wins when no payload present.
    with _pytest.raises(Exception, match="could not read"):
        _decode_b64_payload("<B64_ERROR>cannot read</B64_ERROR>", "/x")
    # No payload, no error marker.
    with _pytest.raises(Exception, match="no <B64> payload"):
        _decode_b64_payload("I could not do that, sorry.", "/x")
    # Garbage payload inside the tags (regex-legible but non-base64 body).
    with _pytest.raises(Exception, match="invalid base64|no <B64> payload"):
        _decode_b64_payload("<B64>@@@not-base64@@@</B64>", "/x")
    # Empty file.
    import base64 as _b64
    with _pytest.raises(Exception, match="empty file"):
        _decode_b64_payload("<B64>" + _b64.b64encode(b"").decode() + "</B64>", "/x")


@pytest.mark.asyncio
async def test_fetch_file_local_direct_read(tmp_path: Path):
    """同机部署：文件存在 → 直接读盘，不发起任何 HTTP。"""
    f = tmp_path / "deck.pptx"
    f.write_bytes(b"PK\x03\x04 fake pptx")
    p = _provider()
    data, media = await p.fetch_file(str(f))
    assert data == b"PK\x03\x04 fake pptx"
    assert "presentationml" in media


@pytest.mark.asyncio
async def test_fetch_file_rejects_relative_path():
    p = _provider()
    try:
        await p.fetch_file("relative/x.txt")
        assert False, "should raise"
    except Exception as exc:
        assert "absolute" in str(exc)


@pytest.mark.asyncio
async def test_fetch_file_remote_via_agent_b64(tmp_path: Path, monkeypatch):
    """异机部署：本地无文件 → 一次 agent 往返取 <B64> 载荷。"""
    import base64 as _b64

    payload = _b64.b64encode(b"remote-file-bytes").decode()
    captured: dict[str, Any] = {}

    async def fake_stream_via_runs(messages, options):
        captured["prompt"] = messages[-1]["content"]
        yield ChatDelta(content=f"<B64>{payload}</B64>")

    async def fake_probe():
        return True

    p = _provider()
    monkeypatch.setattr(p, "_stream_via_runs", fake_stream_via_runs)
    monkeypatch.setattr(p, "_probe_runs_support", fake_probe)

    data, media = await p.fetch_file("/root/remote.txt")
    assert data == b"remote-file-bytes"
    assert media == "text/plain"
    # The prompt pins the exact output format and mentions the path.
    assert "/root/remote.txt" in captured["prompt"]
    assert "<B64>" in captured["prompt"]


@pytest.mark.asyncio
async def test_fetch_file_remote_agent_reports_error(tmp_path: Path, monkeypatch):
    async def fake_stream_via_runs(messages, options):
        yield ChatDelta(content="<B64_ERROR>cannot read</B64_ERROR>")

    async def fake_probe():
        return True

    p = _provider()
    monkeypatch.setattr(p, "_stream_via_runs", fake_stream_via_runs)
    monkeypatch.setattr(p, "_probe_runs_support", fake_probe)

    try:
        await p.fetch_file("/root/missing.txt")
        assert False, "should raise"
    except Exception as exc:
        assert "could not read" in str(exc)
