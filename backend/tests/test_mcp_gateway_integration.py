"""End-to-end: a configured MCP server's tool reaches the model + the gateway.

Wires the full Task 9 path that the static ``MCP_SERVERS`` config + the
``merge_mcp_tools`` seam enable:

  1. A fake in-process MCP server (stdio subprocess) is connected via the live
     :class:`McpClientRegistry` singleton (the same one main.py populates).
  2. The native runtime's merge seam (:func:`merge_mcp_tools`) registers the
     discovered ``echo`` tool into a per-run :class:`ToolRegistry` — so the
     model is OFFERED the namespaced tool (its schema appears in
     ``openai_schemas``).
  3. A :class:`ToolGateway` built with that registry EXECUTES the namespaced
     tool, and the call flows out to the fake MCP server and back — with a
     persisted ``ToolCall`` audit row for ``mcp__<server>__echo``, proving MCP
     tools get the SAME audit treatment as builtins.

No external services and no live model endpoint: the fake MCP server is a
Python subprocess, and the gateway is exercised directly (the exact path the
native runtime calls per tool invocation).
"""
from __future__ import annotations

import sys
import textwrap
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.agents.gateway.tool_gateway import ToolGateway
from app.agents.mcp_client import (
    McpClientRegistry,
    McpServerConfig,
    get_live_mcp_registry,
    merge_mcp_tools,
    set_live_mcp_registry,
)
from app.models import AgentRun, Conversation, Message, ToolCall
from app.tools.registry_init import get_default_registry

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")

# A fake MCP server speaking JSON-RPC over stdin/stdout with one tool: ``echo``.
FAKE_SERVER_SRC = textwrap.dedent(
    """\
    import json, sys

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
                "serverInfo": {"name": "fake-gw", "version": "1.0"},
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
            args = params.get("arguments", {})
            respond(req_id, {"text": args.get("text", "")})
    """
)


@pytest.fixture
def fake_stdio_config(tmp_path):
    script = tmp_path / "fake_mcp_gw.py"
    script.write_text(FAKE_SERVER_SRC, encoding="utf-8")
    return McpServerConfig(
        name="fake-gw",
        command=sys.executable,
        args=[str(script)],
        transport="stdio",
    )


@pytest_asyncio.fixture
async def connected_registry(fake_stdio_config):
    """A live McpClientRegistry connected to the fake server, published as the
    process singleton (the same state main.py lifespan sets up)."""
    registry = McpClientRegistry([fake_stdio_config])
    await registry.connect_all()
    assert registry.catalog.count() == 1, "fake MCP server should expose one tool"
    previous = get_live_mcp_registry()
    set_live_mcp_registry(registry)
    try:
        yield registry
    finally:
        set_live_mcp_registry(previous)
        await registry.disconnect_all()


async def _seed_run(db_session):
    conv = Conversation(user_id=_SEEDED_USER, title="mcp-gw")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id,
        message_id=msg.id,
        user_id=_SEEDED_USER,
        runtime="native",
        flow_name="test",
        status="running",
    )
    db_session.add(run)
    await db_session.flush()
    return conv, msg, run


@pytest.mark.asyncio
async def test_mcp_tool_is_offered_to_model(db_session, connected_registry):
    """The merge seam registers the discovered MCP tool into a per-run registry
    so it appears in the schemas advertised to the model."""
    registry = get_default_registry()
    merged = merge_mcp_tools(registry)  # reads the live singleton
    assert merged == 1

    names = {t.name for t in registry.list()}
    assert "mcp__fake-gw__echo" in names

    schemas = {s["function"]["name"]: s for s in registry.openai_schemas()}
    assert "mcp__fake-gw__echo" in schemas
    fn = schemas["mcp__fake-gw__echo"]["function"]
    assert "[mcp/fake-gw]" in fn["description"]
    assert fn["parameters"]["properties"]["text"]["type"] == "string"


@pytest.mark.asyncio
async def test_mcp_tool_call_flows_through_gateway_with_audit_row(
    db_session, connected_registry
):
    """Invoking the namespaced MCP tool through ToolGateway reaches the fake
    server AND writes the same ToolCall audit row a builtin would."""
    conv, msg, run = await _seed_run(db_session)

    registry = get_default_registry()
    merge_mcp_tools(registry)
    gateway = ToolGateway(
        db_session,
        conversation_id=conv.id,
        assistant_message_id=msg.id,
        run_id=run.id,
        user=None,
        registry=registry,
    )

    result = await gateway.execute(
        tool_call_id="call_mcp_echo",
        tool_name="mcp__fake-gw__echo",
        arguments={"text": "hello-mcp"},
    )

    # The call reached the fake MCP server and echoed back.
    assert result.ok is True
    assert result.status == "success"
    assert "hello-mcp" in result.full_result

    # The gateway persisted an audit row for the namespaced tool — the SAME
    # treatment as a builtin (the headline Task 9 guarantee).
    rows = (
        await db_session.execute(
            select(ToolCall).where(ToolCall.tool_name == "mcp__fake-gw__echo")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].arguments == {"text": "hello-mcp"}
    assert rows[0].conversation_id == conv.id


@pytest.mark.asyncio
async def test_mcp_merge_is_noop_when_unconfigured(db_session):
    """Boot guard: with no live registry published, merge_mcp_tools is a no-op
    and the default registry stays builtins-only (no crash, no MCP tools)."""
    previous = get_live_mcp_registry()
    set_live_mcp_registry(None)
    try:
        registry = get_default_registry()
        merged = merge_mcp_tools(registry)
        assert merged == 0
        names = {t.name for t in registry.list()}
        assert not any(n.startswith("mcp__") for n in names)
    finally:
        set_live_mcp_registry(previous)
