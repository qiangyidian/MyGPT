"""MCP catalog: namespacing + provenance + OpenAI schema render."""
from __future__ import annotations

import pytest

from app.agents.mcp_catalog import McpCatalog, McpToolInfo, namespace


def test_namespace_format():
    assert namespace("github", "create_issue") == "mcp__github__create_issue"


def test_register_aggregates_and_namespaces():
    cat = McpCatalog()
    cat.register("github", [McpToolInfo(server="github", name="create_issue", description="x")])
    cat.register("slack", [McpToolInfo(server="slack", name="post", description="y")])
    names = {t.namespaced_name for t in cat.all()}
    assert names == {"mcp__github__create_issue", "mcp__slack__post"}
    assert cat.count() == 2


def test_provenance_source_tagged():
    cat = McpCatalog()
    cat.register("srv", [McpToolInfo(server="srv", name="t", description="d")], source="plugin")
    t = cat.get("mcp__srv__t")
    assert t is not None and t.source == "plugin"


def test_later_registration_overrides_on_clash():
    cat = McpCatalog()
    cat.register("srv", [McpToolInfo(server="srv", name="t", description="config-ver")], source="config")
    cat.register("srv", [McpToolInfo(server="srv", name="t", description="plugin-ver")], source="plugin")
    assert cat.count() == 1
    assert cat.get("mcp__srv__t").description == "plugin-ver"
    assert cat.get("mcp__srv__t").source == "plugin"


def test_openai_schemas_namespaced_with_server_tag():
    cat = McpCatalog()
    cat.register("db", [McpToolInfo(server="db", name="query", description="run sql",
                                    input_schema={"type": "object", "properties": {"q": {"type": "string"}}})])
    schemas = cat.to_openai_schemas()
    assert len(schemas) == 1
    fn = schemas[0]["function"]
    assert fn["name"] == "mcp__db__query"
    assert "[mcp/db]" in fn["description"]
    assert fn["parameters"]["properties"]["q"]["type"] == "string"


# --------------------------------------------------------------------------- #
# Gateway routing: McpToolWrapper delegates to the session + carries provenance
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_mcp_tool_wrapper_delegates_to_session():
    """A wrapped MCP tool's run() calls the owning session with the args, and
    its schema surfaces the namespaced name + server-provenance tag."""
    from app.agents.mcp_client import McpToolWrapper

    captured: dict = {}

    async def fake_caller(tool_name, arguments):
        captured["tool"] = tool_name
        captured["args"] = arguments
        return {"echoed": arguments.get("text")}

    wrapper = McpToolWrapper(
        namespaced_name="mcp__echo__echo",
        server="echo",
        tool_name="echo",
        description="echo text",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        caller=fake_caller,
    )

    # Schema carries the namespaced name + server tag (provenance for the model).
    schema = wrapper.to_openai_schema()
    assert schema["function"]["name"] == "mcp__echo__echo"
    assert "[mcp/echo]" in schema["function"]["description"]
    assert schema["function"]["parameters"]["properties"]["text"]["type"] == "string"

    # run() delegates to the injected caller (which would route through the
    # gateway's execute path in production).
    result = await wrapper.run(text="hi")
    assert result == {"echoed": "hi"}
    assert captured == {"tool": "echo", "args": {"text": "hi"}}


def test_mcp_tool_wrapper_is_not_dangerous_by_default():
    """MCP tools flow through the gateway's audit/truncation/budget path; they
    are not auto-marked dangerous unless a caller opts a write tool in."""
    from app.agents.mcp_client import McpToolWrapper

    async def _noop(tool_name, arguments):
        return {}

    w = McpToolWrapper(
        namespaced_name="mcp__s__t",
        server="s",
        tool_name="t",
        description="d",
        input_schema={},
        caller=_noop,
    )
    assert w.dangerous is False


@pytest.mark.asyncio
async def test_build_gateway_tools_wraps_every_catalogued_tool():
    """build_gateway_tools emits one McpToolWrapper per catalogued tool, each
    delegating to registry.call_tool under the namespaced name."""
    from app.agents.mcp_client import McpClientRegistry, build_gateway_tools

    reg = McpClientRegistry()
    # Feed pre-discovered tools without connecting (static registration).
    reg.register_static(
        "echo",
        [McpToolInfo(server="echo", name="echo", description="e",
                     input_schema={"type": "object", "properties": {"text": {"type": "string"}}})],
    )
    reg._connected = True  # allow call_tool past the guard

    captured: dict = {}

    async def fake_call_tool(namespaced, arguments):
        captured["namespaced"] = namespaced
        captured["args"] = arguments
        return {"ok": True}

    reg.call_tool = fake_call_tool  # type: ignore[method-assign]

    wrappers = build_gateway_tools(reg)
    assert len(wrappers) == 1
    assert wrappers[0].name == "mcp__echo__echo"
    result = await wrappers[0].run(text="x")
    assert result == {"ok": True}
    assert captured == {"namespaced": "mcp__echo__echo", "args": {"text": "x"}}
