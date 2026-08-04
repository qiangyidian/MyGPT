"""MCP tool catalog — aggregate tools from multiple MCP servers with provenance.

Codex pattern (from ``codex-mcp``): every connected MCP server's tools are
aggregated into ONE flat list, namespaced ``mcp__<server>__<tool>``, and each
tool carries *provenance* (which server + whether it came from user config vs a
plugin) so plugin-provided tools are distinguishable and governable.

This module is the catalog/aggregation core (pure, unit-testable). The live
connection transport (stdio/SSE MCP sessions) lives in :mod:`app.agents.mcp_client`
and is optional — the catalog works equally well fed by a fake client in tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Source = Literal["config", "plugin"]


def namespace(server: str, tool: str) -> str:
    """The stable, namespaced tool id seen by the model."""
    return f"mcp__{server}__{tool}"


@dataclass
class McpToolInfo:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    source: Source = "config"

    @property
    def namespaced_name(self) -> str:
        return namespace(self.server, self.name)


class McpCatalog:
    """Aggregates tools from multiple servers; dedups by namespaced name.

    Later registrations override earlier on a namespaced-name clash (e.g. a
    plugin shadowing a config tool) while keeping the latest provenance.
    """

    def __init__(self) -> None:
        self._tools: dict[str, McpToolInfo] = {}

    def register(self, server: str, tools: list[McpToolInfo], source: Source = "config") -> None:
        for t in tools:
            t.source = source
            t.server = server
            self._tools[t.namespaced_name] = t  # latest wins

    def all(self) -> list[McpToolInfo]:
        return list(self._tools.values())

    def by_server(self, server: str) -> list[McpToolInfo]:
        return [t for t in self._tools.values() if t.server == server]

    def get(self, namespaced_name: str) -> McpToolInfo | None:
        return self._tools.get(namespaced_name)

    def to_openai_schemas(self) -> list[dict[str, Any]]:
        """Render the catalog as OpenAI tool schemas for the registry adapter."""
        out: list[dict[str, Any]] = []
        for t in self._tools.values():
            out.append({
                "type": "function",
                "function": {
                    "name": t.namespaced_name,
                    "description": f"[mcp/{t.server}] {t.description}".strip(),
                    "parameters": t.input_schema or {"type": "object", "properties": {}},
                },
            })
        return out

    def count(self) -> int:
        return len(self._tools)
