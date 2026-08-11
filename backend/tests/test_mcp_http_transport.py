"""MCP Streamable HTTP transport: JSON-RPC over httpx.

The fake MCP server is a minimal ASGI app driven through httpx's ASGITransport,
so the whole test runs in-process with no listening socket and no external
service. These tests pin the HTTP transport contract: initialize negotiation,
tools/list, tools/call, cancellation, per-request timeout, and graceful close.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.agents.mcp_transport import (
    HttpTransport,
    McpSession,
    McpTimeoutError,
    McpToolDef,
)


def _make_fake_mcp_app(*, slow_delay: float = 30.0):
    """Build a minimal ASGI app speaking the MCP JSON-RPC protocol.

    ``slow_delay`` controls how long the ``slow`` tool blocks, so the timeout
    and cancellation tests don't have to wait minutes.
    """

    async def app(scope, receive, send):
        # Only handle http requests.
        if scope["type"] != "http":
            return
        body = b""
        more = True
        while more:
            msg = await receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
        try:
            req = json.loads(body.decode() or "{}")
        except Exception:
            req = {}
        method = req.get("method")
        req_id = req.get("id")
        params = req.get("params", {}) or {}
        result: object
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-http", "version": "1.0"},
            }
        elif method == "tools/list":
            result = {"tools": [{
                "name": "echo",
                "description": "echo the text back",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }]}
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments", {}) or {}
            if name == "echo":
                result = {"text": args.get("text", "")}
            elif name == "slow":
                await asyncio.sleep(slow_delay)
                result = {"done": True}
            else:
                result = {"error": "unknown tool"}
        else:
            result = {}

        payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}).encode()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": payload})

    return app


def _http_client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fake-mcp")


@pytest.fixture
def fake_http_server():
    app = _make_fake_mcp_app()
    client = _http_client_for(app)
    transport = HttpTransport(url="http://fake-mcp/mcp", client=client)
    return type("FakeHttp", (), {"transport": transport, "client": client, "app": app})()


@pytest.mark.asyncio
async def test_http_mcp_discovers_and_calls_tool(fake_http_server):
    client = McpSession(transport=fake_http_server.transport)
    try:
        await client.initialize()
        tools = await client.list_tools()
        assert len(tools) == 1
        assert isinstance(tools[0], McpToolDef)
        assert tools[0].name == "echo"
        result = await client.call_tool("echo", {"text": "ok"})
        assert result == {"text": "ok"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_initialize_negotiates_protocol(fake_http_server):
    client = McpSession(transport=fake_http_server.transport)
    try:
        info = await client.initialize()
        assert info["protocolVersion"] == "2024-11-05"
        assert info["serverInfo"]["name"] == "fake-http"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_cancellation_aborts_inflight_call(fake_http_server):
    client = McpSession(transport=fake_http_server.transport)
    try:
        await client.initialize()
        task = asyncio.create_task(client.call_tool("slow", {}))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Session still usable.
        assert await client.call_tool("echo", {"text": "after"}) == {"text": "after"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_timeout_fires_on_unresponsive_call():
    # Make the slow tool genuinely block long enough that the per-request
    # timeout fires first.
    app = _make_fake_mcp_app(slow_delay=30.0)
    transport = HttpTransport(url="http://fake-mcp/mcp", client=_http_client_for(app))
    client = McpSession(transport=transport, request_timeout=0.5)
    try:
        await client.initialize()
        with pytest.raises(McpTimeoutError):
            await client.call_tool("slow", {})
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_graceful_close_is_idempotent(fake_http_server):
    client = McpSession(transport=fake_http_server.transport)
    await client.initialize()
    await client.close()
    # A second close must not raise.
    await client.close()
    assert fake_http_server.transport.closed
