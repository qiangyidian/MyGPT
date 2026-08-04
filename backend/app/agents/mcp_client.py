"""Optional MCP client adapter (Codex pattern).

Connects to configured MCP servers, aggregates their tools into the catalog
(:mod:`app.agents.mcp_catalog`), and routes tool calls back to the owning server.

Guarded by design: if the ``mcp`` SDK isn't importable or no servers are
configured, every method no-ops and the app boots unchanged. The catalog is the
provenance/aggregation core (unit-tested); this module is the live-connection
layer. Full transport wiring (stdio/SSE session lifecycle) needs a real MCP
server to validate, so connection is lazy and failure-isolated per server.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.agents.mcp_catalog import McpCatalog, McpToolInfo

logger = logging.getLogger(__name__)

try:  # optional dependency
    import mcp as _mcp  # type: ignore  # noqa: F401
    _MCP_AVAILABLE = True
except Exception:  # noqa: BLE001
    _MCP_AVAILABLE = False


@dataclass
class McpServerConfig:
    """One MCP server connection (stdio transport by default)."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"  # "stdio" | "sse" | "http"


class McpClientRegistry:
    """Aggregates tools from all configured MCP servers into one catalog."""

    def __init__(self, servers: list[McpServerConfig] | None = None) -> None:
        self._servers = list(servers or [])
        self._catalog = McpCatalog()
        self._sessions: dict[str, Any] = {}  # server name -> live session
        self._connected = False

    @property
    def enabled(self) -> bool:
        return bool(self._servers) and _MCP_AVAILABLE

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
        # NOTE: full transport wiring (mcp.ClientSession over stdio/sse) requires a
        # live server to validate and is intentionally left as the integration
        # point — populate self._catalog from each session's list_tools() here.
        for srv in self._servers:
            try:
                await self._connect_one(srv)
            except Exception:  # noqa: BLE001 — isolate per-server failures
                logger.warning("mcp server %s failed to connect; skipped", srv.name, exc_info=True)
        self._connected = True
        return self._catalog

    async def _connect_one(self, srv: McpServerConfig) -> None:
        """Connect to one server and add its tools to the catalog.

        Concrete transport implementation goes here (open stdio/SSE, call
        list_tools, translate to McpToolInfo). Left as a documented hook so the
        module is shippable without a live MCP server in CI.
        """
        logger.info("mcp connect (%s, %s): transport wiring pending", srv.name, srv.transport)

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
        # Delegate to the live session: return await session.call_tool(info.name, arguments)
        raise NotImplementedError("live mcp call_tool requires transport wiring")

    async def disconnect_all(self) -> None:
        for name, session in list(self._sessions.items()):
            try:
                close = getattr(session, "aclose", None) or getattr(session, "close", None)
                if close is not None:
                    res = close()
                    if hasattr(res, "__await__"):
                        await res
            except Exception:  # noqa: BLE001
                logger.warning("mcp disconnect %s failed", name, exc_info=True)
        self._sessions.clear()
        self._connected = False
