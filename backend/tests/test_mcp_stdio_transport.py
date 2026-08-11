"""MCP stdio transport: real JSON-RPC over a subprocess stdin/stdout.

These tests run against a tiny in-process fake MCP server (a Python script that
speaks the JSON-RPC line protocol). No external services, no optional ``mcp``
SDK required. They pin the transport contract: initialize negotiation,
tools/list, tools/call, cancellation, per-request timeout, reconnect after a
dropped subprocess, and bounded stderr capture.
"""
from __future__ import annotations

import asyncio
import sys
import textwrap

import pytest

from app.agents.mcp_client import McpServerConfig
from app.agents.mcp_transport import (
    McpSession,
    McpTimeoutError,
    McpToolDef,
    StdioTransport,
)

# A fake MCP server speaking JSON-RPC over stdin/stdout. It implements the
# methods the transport exercises: initialize, tools/list, tools/call (echo /
# slow / stderr_spam). Notifications (initialized, cancelled) are accepted and
# ignored. The echo tool returns its argument verbatim so the transport's
# result correlation is observable without MCP CallToolResult wrapping.
FAKE_SERVER_SRC = textwrap.dedent(
    """\
    import json, sys, time

    def respond(req_id, result):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            respond(req_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-stdio", "version": "1.0"},
            })
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            respond(req_id, {"tools": [{
                "name": "echo",
                "description": "echo the text back",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }]})
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "echo":
                respond(req_id, {"text": args.get("text", "")})
            elif name == "slow":
                time.sleep(30)
                respond(req_id, {"done": True})
            elif name == "stderr_spam":
                sys.stderr.write("E" * 200000 + "\\n")
                sys.stderr.flush()
                respond(req_id, {"ok": True})
            else:
                respond(req_id, {"error": "unknown tool"})
        # notifications/cancelled: ignored
    """
)


@pytest.fixture
def fake_stdio_server(tmp_path):
    script = tmp_path / "fake_mcp_stdio.py"
    script.write_text(FAKE_SERVER_SRC, encoding="utf-8")
    config = McpServerConfig(
        name="fake-stdio",
        command=sys.executable,
        args=[str(script)],
        transport="stdio",
    )
    holder = type("FakeStdio", (), {"config": config, "script": script})()
    return holder


@pytest.mark.asyncio
async def test_stdio_mcp_discovers_and_calls_tool(fake_stdio_server):
    client = McpSession(fake_stdio_server.config)
    try:
        await client.initialize()
        tools = await client.list_tools()
        assert len(tools) == 1
        assert isinstance(tools[0], McpToolDef)
        assert tools[0].name == "echo"
        assert "text" in tools[0].input_schema.get("properties", {})
        result = await client.call_tool("echo", {"text": "ok"})
        assert result == {"text": "ok"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_initialize_negotiates_protocol(fake_stdio_server):
    client = McpSession(fake_stdio_server.config)
    try:
        info = await client.initialize()
        assert info["protocolVersion"] == "2024-11-05"
        assert info["serverInfo"]["name"] == "fake-stdio"
        assert "tools" in info["capabilities"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_cancellation_aborts_inflight_call(fake_stdio_server):
    client = McpSession(fake_stdio_server.config)
    try:
        await client.initialize()
        task = asyncio.create_task(client.call_tool("slow", {}))
        # Give the request time to be written and start pending.
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # After cancellation the session must still be usable for other calls.
        result = await client.call_tool("echo", {"text": "after-cancel"})
        assert result == {"text": "after-cancel"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_timeout_fires_on_unresponsive_call(fake_stdio_server):
    client = McpSession(fake_stdio_server.config, request_timeout=0.5)
    try:
        await client.initialize()
        with pytest.raises(McpTimeoutError):
            await client.call_tool("slow", {})
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_reconnect_after_dropped_subprocess(fake_stdio_server):
    client = McpSession(fake_stdio_server.config)
    try:
        await client.initialize()
        assert await client.call_tool("echo", {"text": "first"}) == {"text": "first"}
        # Kill the underlying subprocess to simulate a dropped server.
        await client.transport.kill()
        assert client.transport.closed
        # Reconnect spawns a fresh subprocess.
        await client.reconnect()
        await client.initialize()
        assert await client.call_tool("echo", {"text": "back"}) == {"text": "back"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_stderr_capture_is_bounded(fake_stdio_server):
    transport = StdioTransport(
        command=sys.executable,
        args=[str(fake_stdio_server.script)],
        stderr_limit=4096,
    )
    client = McpSession(transport=transport)
    try:
        await client.initialize()
        await client.call_tool("stderr_spam", {})
        # Let the stderr drain into the ring buffer.
        for _ in range(20):
            await asyncio.sleep(0.05)
            if len(transport.stderr_tail) >= 4096:
                break
        assert len(transport.stderr_tail) <= 4096
        # Content is the spam marker plus a trailing newline (which on Windows
        # may be "\r\n"). Just assert it's bounded and dominated by the marker.
        assert "E" in transport.stderr_tail
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_graceful_shutdown_terminates_subprocess(fake_stdio_server):
    client = McpSession(fake_stdio_server.config)
    await client.initialize()
    transport = client.transport
    await client.close()
    assert transport.closed
    assert transport._proc is None or transport._proc.returncode is not None
