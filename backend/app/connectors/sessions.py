"""Per-tenant connector→session lifecycle (Task 9 follow-up).

Opens a live :class:`~app.agents.mcp_transport.McpSession` for each of a user's
**ENABLED** connectors, discovers its tools, and exposes them through the SAME
:class:`~app.agents.mcp_client.McpClientRegistry` /
:class:`~app.agents.mcp_client.McpToolWrapper` gateway path the static
``MCP_SERVERS`` config uses — so connector tools route through
:class:`~app.agents.gateway.tool_gateway.ToolGateway` (approval / audit /
truncation / budget) like built-ins.

Security core (tenant isolation):
    A :class:`ConnectorSessionManager` is built per run and loads ONLY the
    requesting user's connectors via
    :meth:`~app.connectors.service.ConnectorService.list_for_user` (which
    filters by ``user_id``). Another user's connectors are never loaded, never
    connected, never offered. The merged tools'
    :class:`~app.agents.mcp_client.McpToolWrapper` call back into THAT user's
    :class:`McpClientRegistry` only.

Graceful degradation:
    A connector whose session fails to initialize is isolated (logged + skipped)
    via :meth:`McpClientRegistry.connect_all`'s per-server try/except. The rest
    of the run and the other connectors are unaffected.

Credential hygiene:
    Credentials are decrypted in-memory only (via
    :meth:`~app.connectors.service.ConnectorService.decrypted_credentials` →
    :meth:`~app.connectors.service.ConnectorService.build_server_config`) and
    placed in the session ``env``. Plaintext is never persisted and never
    logged. Sessions are closed at run end (:meth:`close_all`), discarding
    in-memory state.

Lifecycle note:
    Per-request open/close is fine for now. A connection pool / cache that keeps
    sessions warm across turns (and connects to a user's servers concurrently
    rather than sequentially) is a later optimization — out of scope here.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.service import ConnectorService

logger = logging.getLogger(__name__)


class ConnectorSessionManager:
    """Per-run, tenant-scoped connector→MCP-session lifecycle.

    Construct one per run (or per request), call :meth:`open_for_user` to get an
    :class:`~app.agents.mcp_client.McpClientRegistry` whose catalog holds the
    user's enabled-connector tools, merge those tools into the run's
    :class:`~app.tools.base.ToolRegistry` via
    :func:`~app.agents.mcp_client.merge_mcp_tools`, and call :meth:`close_all`
    in a ``finally`` at run end so sessions are torn down (graceful shutdown).
    """

    def __init__(
        self,
        db: AsyncSession,
        *,
        session_factory: Any | None = None,
    ) -> None:
        self._db = db
        self._service = ConnectorService(db)
        # None → McpClientRegistry builds real McpSession instances per config
        # (the production path). Tests inject a fake that spawns no subprocess.
        self._session_factory = session_factory
        # The last-opened registry (held so :meth:`close_all` can tear it down
        # without the caller having to thread it back through). ``open_for_user``
        # is idempotent: a second call returns this cached registry rather than
        # re-opening — important for the CrewAI multi-stage path, where
        # ``_build_tools`` runs once per stage but sessions should open once.
        self._registry: Any | None = None

    async def open_for_user(self, user_id: uuid.UUID) -> Any:
        """Load the user's ENABLED connectors, open a session per connector,
        and return an :class:`McpClientRegistry` with their tools catalogued.

        Idempotent: a second call returns the already-opened registry (so the
        CrewAI multi-stage path, which rebuilds tools per stage, reuses one set
        of sessions instead of re-opening). Returns an empty (no-op) registry
        when the user has no enabled connectors — so the run is unaffected.
        Per-connector failures are isolated by
        :meth:`McpClientRegistry.connect_all`; a broken connector never crashes
        the run.
        """
        if self._registry is not None:
            return self._registry

        # Lazy import avoids a startup cycle (mcp_client imports the transport,
        # which is heavier than this module needs at import time).
        from app.agents.mcp_client import McpClientRegistry

        try:
            connectors = await self._service.list_for_user(user_id)
        except Exception:
            logger.warning(
                "connector list for user %s failed; no connector tools",
                user_id,
                exc_info=True,
            )
            self._registry = McpClientRegistry([])
            return self._registry

        enabled = [c for c in connectors if c.enabled]
        if not enabled:
            self._registry = McpClientRegistry([])
            return self._registry

        # Materialize configs (decrypt creds IN MEMORY ONLY — they live in the
        # session env, never persisted, never logged).
        configs: list[Any] = []
        for conn in enabled:
            try:
                configs.append(await self._service.build_server_config(conn))
            except Exception:
                logger.warning(
                    "connector %s (%s) config build failed; skipped",
                    conn.id, conn.provider, exc_info=True,
                )

        if not configs:
            self._registry = McpClientRegistry([])
            return self._registry

        kwargs: dict[str, Any] = {}
        if self._session_factory is not None:
            kwargs["session_factory"] = self._session_factory
        registry = McpClientRegistry(configs, **kwargs)
        # connect_all opens each session, runs initialize + tools/list, and
        # registers tools into the catalog. Per-server failure is isolated
        # (logged + skipped) inside connect_all — a broken connector never
        # breaks the others or the run.
        try:
            await registry.connect_all()
        except Exception:
            logger.warning(
                "connector connect_all failed for user %s; partial tools",
                user_id, exc_info=True,
            )
        # Record real usage (B8): stamp last_used_at on the connectors whose
        # sessions actually opened, so the settings page's "最近使用" reflects
        # reality. Best-effort — a stamp failure never breaks the run.
        try:
            from datetime import datetime, timezone

            from sqlalchemy import update

            from app.connectors.models import Connector

            used_ids = [c.id for c in enabled]
            if used_ids:
                await self._db.execute(
                    update(Connector)
                    .where(Connector.id.in_(used_ids))
                    .values(last_used_at=datetime.now(timezone.utc))
                )
                await self._db.commit()
        except Exception:
            logger.debug("last_used_at stamp failed", exc_info=True)
        logger.info(
            "connector sessions opened for user %s: %d server(s), %d tool(s)",
            user_id, len(configs), registry.catalog.count(),
        )
        self._registry = registry
        return registry

    async def close_all(self, registry: Any | None = None) -> None:
        """Gracefully close every opened session (run-end teardown).

        ``registry`` defaults to the registry last opened by
        :meth:`open_for_user`. Idempotent and failure-isolated: a close error on
        one session never raises (``disconnect_all`` already swallows
        per-session errors). Safe to call with an empty/no-op registry or after
        a second close.
        """
        reg = registry if registry is not None else self._registry
        if reg is None:
            return
        try:
            await reg.disconnect_all()
        except Exception:
            logger.warning("connector session close failed", exc_info=True)
        finally:
            self._registry = None


__all__ = ["ConnectorSessionManager"]
