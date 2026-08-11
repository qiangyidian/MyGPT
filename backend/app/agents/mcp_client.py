"""MCP client adapter (Codex pattern).

Connects to configured MCP servers, aggregates their tools into the catalog
(:mod:`app.agents.mcp_catalog`), and routes tool calls back to the owning
server via the real JSON-RPC transports in :mod:`app.agents.mcp_transport`.

Guarded by design: if no servers are configured, every method no-ops and the
app boots unchanged. The catalog is the provenance/aggregation core
(unit-tested); this module is the live-connection layer. Connection is lazy
and per-server failure-isolated, so one unreachable server never blocks the
others or the app boot.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.agents.mcp_catalog import McpCatalog, McpToolInfo
from app.agents.mcp_transport import McpSession

logger = logging.getLogger(__name__)


@dataclass
class McpServerConfig:
    """One MCP server connection (stdio transport by default).

    For HTTP transports, ``command`` holds the server URL and ``transport`` is
    ``"http"`` (or ``"sse"``).
    """

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"  # "stdio" | "sse" | "http"


def _default_session_factory(config: "McpServerConfig") -> McpSession:
    """Default session builder: a real :class:`McpSession` over the config."""
    return McpSession(config)


class McpClientRegistry:
    """Aggregates tools from all configured MCP servers into one catalog."""

    def __init__(
        self,
        servers: list[McpServerConfig] | None = None,
        *,
        session_factory: "Any | None" = None,
    ) -> None:
        self._servers = list(servers or [])
        self._catalog = McpCatalog()
        self._sessions: dict[str, McpSession] = {}  # server name -> live session
        self._connected = False
        # Injectable so the per-tenant connector lifecycle can substitute fake
        # sessions in tests (and so a future cache/pool can return warm
        # sessions). Defaults to building a real McpSession per config — the
        # static MCP_SERVERS path is byte-identical to before.
        self._session_factory = session_factory or _default_session_factory

    @property
    def enabled(self) -> bool:
        # The transport now implements JSON-RPC directly (no optional SDK), so
        # we are enabled whenever servers are configured.
        return bool(self._servers)

    @property
    def catalog(self) -> McpCatalog:
        return self._catalog

    async def connect_all(self) -> McpCatalog:
        """Connect to every configured server and populate the catalog.

        Per-server failure is isolated: one unreachable server doesn't block the
        others. No-op (returns empty catalog) when disabled.
        """
        if not self.enabled:
            return self._catalog
        for srv in self._servers:
            try:
                await self._connect_one(srv)
            except Exception:  # noqa: BLE001 — isolate per-server failures
                logger.warning(
                    "mcp server %s failed to connect; skipped", srv.name, exc_info=True
                )
        self._connected = True
        return self._catalog

    async def _connect_one(self, srv: McpServerConfig) -> None:
        """Open a real transport session and add the server's tools to the catalog."""
        session = self._session_factory(srv)
        await session.initialize()
        tools_raw = await session.list_tools()
        infos = [
            McpToolInfo(
                server=srv.name,
                name=t.name,
                description=t.description,
                input_schema=t.input_schema or {"type": "object", "properties": {}},
                source="config",
            )
            for t in tools_raw
        ]
        self._catalog.register(srv.name, infos, source="config")
        self._sessions[srv.name] = session
        logger.info("mcp server %s connected: %d tools", srv.name, len(infos))

    def register_static(self, server: str, tools: list[McpToolInfo], source: str = "config") -> None:
        """Inject pre-discovered tools (e.g. from a cached manifest) without connecting."""
        self._catalog.register(server, tools, source=source)  # type: ignore[arg-type]

    async def call_tool(self, namespaced_name: str, arguments: dict[str, Any]) -> Any:
        """Route a namespaced tool call to its owning server session."""
        if not self._connected:
            raise RuntimeError("mcp registry not connected; call connect_all() first")
        info = self._catalog.get(namespaced_name)
        if info is None:
            raise KeyError(f"unknown mcp tool: {namespaced_name}")
        session = self._sessions.get(info.server)
        if session is None:
            raise RuntimeError(f"mcp server {info.server} not connected")
        return await session.call_tool(info.name, arguments)

    async def disconnect_all(self) -> None:
        for name, session in list(self._sessions.items()):
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                logger.warning("mcp disconnect %s failed", name, exc_info=True)
        self._sessions.clear()
        self._connected = False


# --------------------------------------------------------------------------- #
# Gateway routing: wrap each catalogued MCP tool as a BaseTool so it flows
# through ToolGateway (approval, audit, truncation, budget) like a builtin.
# --------------------------------------------------------------------------- #
from app.tools.base import BaseTool as _BaseTool  # noqa: E402  (late to keep the cycle clean)


class McpToolWrapper(_BaseTool):
    """A :class:`~app.tools.base.BaseTool` adapter over one MCP tool.

    Subclasses ``BaseTool`` so it satisfies ``isinstance(tool, BaseTool)`` and
    the registry's type checks. ``run(**kwargs)`` delegates to the owning
    :class:`McpSession` via a caller-supplied async callable (so the wrapper is
    decoupled from the registry and unit-testable with a fake). The tool
    surfaces under its namespaced name (``mcp__<server>__<tool>``) and carries
    the server's validated JSON schema, so it routes through
    :class:`ToolGateway` with the same approval / audit / truncation / budget
    treatment as builtins.

    ``dangerous`` defaults to False (the gateway still audits + truncates every
    call); callers may flip it on for write-side tools that should require
    human approval.
    """

    # BaseTool class attributes; overridden per-instance in __init__.
    name: str = ""
    description: str = ""
    category: str = "mcp"
    dangerous: bool = False

    def __init__(
        self,
        *,
        namespaced_name: str,
        server: str,
        tool_name: str,
        description: str,
        input_schema: dict[str, Any],
        caller: Any,
        dangerous: bool = False,
    ) -> None:
        self.name = namespaced_name
        self._server = server
        self._tool_name = tool_name
        self.description = f"[mcp/{server}] {description}".strip()
        self._input_schema = input_schema or {"type": "object", "properties": {}}
        self._caller = caller  # async (tool_name, arguments) -> result
        self.dangerous = bool(dangerous)

    async def run(self, **kwargs: Any) -> Any:
        """Delegate to the owning MCP session through the injected caller."""
        return await self._caller(self._tool_name, kwargs)

    def to_openai_schema(self) -> dict[str, Any]:
        """Render the MCP tool's (already-validated) JSON schema for the model."""
        schema = dict(self._input_schema or {"type": "object", "properties": {}})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }


def build_gateway_tools(
    registry: McpClientRegistry,
) -> list[McpToolWrapper]:
    """Build :class:`McpToolWrapper` adapters for every catalogued tool.

    Each wrapper delegates to :meth:`McpClientRegistry.call_tool` so execution
    flows through the gateway (approval / audit / truncation / budget). Returns
    an empty list when the registry has no connected servers (no-op guard).
    """
    wrappers: list[McpToolWrapper] = []
    for info in registry.catalog.all():
        def _make(namespaced: str) -> Any:
            async def _caller(tool_name: str, arguments: dict[str, Any]) -> Any:
                return await registry.call_tool(namespaced, arguments)

            return _caller

        wrappers.append(
            McpToolWrapper(
                namespaced_name=info.namespaced_name,
                server=info.server,
                tool_name=info.name,
                description=info.description,
                input_schema=info.input_schema,
                caller=_make(info.namespaced_name),
            )
        )
    return wrappers


def merge_mcp_tools(
    target: Any,
    mcp_registry: "McpClientRegistry | None" = None,
) -> int:
    """Register every catalogued MCP tool into ``target`` (a ToolRegistry).

    The merge seam called by both runtimes at turn start: it takes the live MCP
    registry (defaults to the process singleton), builds gateway wrappers for
    each connected tool, and registers them into ``target`` so the model is
    offered them and calls flow through :class:`ToolGateway`. Returns the number
    of tools merged. A no-op (returns 0) when MCP is unconfigured or
    disconnected — the boot guard holds.
    """
    if mcp_registry is None:
        mcp_registry = _live_mcp_registry()
    if mcp_registry is None or not mcp_registry.enabled:
        return 0
    count = 0
    for wrapper in build_gateway_tools(mcp_registry):
        try:
            target.register(wrapper)
            count += 1
        except Exception:  # noqa: BLE001 — registration must not break the turn
            logger.warning("failed to register mcp tool %s", getattr(wrapper, "name", "?"), exc_info=True)
    return count


# --------------------------------------------------------------------------- #
# Process singleton for the live (static) MCP registry, populated by main.py
# lifespan and read by the runtimes. Mirrors the approval_bus singleton pattern.
# --------------------------------------------------------------------------- #
_LIVE_MCP_REGISTRY: "McpClientRegistry | None" = None


def set_live_mcp_registry(registry: "McpClientRegistry | None") -> None:
    """Populate the process-wide live MCP registry (called from main lifespan)."""
    global _LIVE_MCP_REGISTRY
    _LIVE_MCP_REGISTRY = registry


def get_live_mcp_registry() -> "McpClientRegistry | None":
    """Return the live MCP registry set by main.py, or None (no-op guard)."""
    return _LIVE_MCP_REGISTRY


def _live_mcp_registry() -> "McpClientRegistry | None":
    return _LIVE_MCP_REGISTRY


def build_static_configs(raw: str) -> list[McpServerConfig]:
    """Parse the ``MCP_SERVERS`` JSON string into config objects.

    Tolerant: a blank/malformed value yields an empty list (the boot guard
    holds — the app never crashes on a bad MCP config). Each entry may carry
    name, command, args, env, transport.
    """
    if not raw or not raw.strip():
        return []
    import json as _json

    try:
        data = _json.loads(raw)
    except _json.JSONDecodeError:
        logger.warning("MCP_SERVERS is not valid JSON; ignoring static MCP servers")
        return []
    if not isinstance(data, list):
        return []
    out: list[McpServerConfig] = []
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("command"):
            continue
        out.append(
            McpServerConfig(
                name=str(entry["name"]),
                command=str(entry["command"]),
                args=list(entry.get("args") or []),
                env=dict(entry.get("env") or {}),
                transport=str(entry.get("transport") or "stdio"),
            )
        )
    return out
