"""MCP catalog: namespacing + provenance + OpenAI schema render."""
from __future__ import annotations

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
