"""Connector service: encrypted, tenant-scoped CRUD + scope enforcement.

This is the single entry point for managing connector definitions. It enforces:

  * **Encryption at rest** — credentials are Fernet-encrypted via
    :mod:`app.core.security` before they ever touch the DB. Only
    :meth:`decrypted_credentials` returns the plaintext, in memory, for the
    active session.
  * **Rotation** — :meth:`rotate_credentials` replaces the ciphertext with a
    fresh encryption of the new payload.
  * **Tenant isolation** — every query filters by ``user_id``; a connector
    owned by user A is invisible (and unoperable) by user B.
  * **Minimum OAuth scopes** — :meth:`enable` refuses to enable a connector
    whose ``oauth_scopes`` don't cover the catalog manifest's
    ``required_scopes``, and :meth:`update` re-runs that gate when scopes
    change (auto-disabling a connector that drops below the minimum).

Scope of wiring (Task 9): this service owns connector *definitions* only. The
live static-MCP path (``MCP_SERVERS``) is fully wired into both agent
runtimes via :func:`app.agents.mcp_client.merge_mcp_tools`. Per-tenant
connector→session wiring (opening a live :class:`McpSession` for each enabled
connector row, scoped to that user, at turn time) is a **follow-up**: the
``build_server_config`` helper below is the documented seam — it materializes
an :class:`McpServerConfig` (transport + command/URL + decrypted credentials
as env) ready to hand to :class:`McpSession`, but the per-user session
lifecycle manager that calls it is not yet implemented.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.catalog import get_manifest
from app.connectors.models import Connector
from app.core.exceptions import AppException
from app.core.security import decrypt_secret, encrypt_secret
from app.observability import observe_counter, observe_span

logger = logging.getLogger(__name__)


class InsufficientScopesError(Exception):
    """Raised when enabling a connector whose scopes miss a required scope."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f"missing required OAuth scopes: {', '.join(missing)}")


class ConnectorNotFoundError(Exception):
    """Raised when a tenant-scoped lookup misses (no row OR wrong tenant)."""


class StdioConnectorForbiddenError(Exception):
    """Raised when a non-admin tries to register a ``stdio`` connector.

    A stdio connector's ``command_or_url`` is executed server-side via
    ``asyncio.create_subprocess_exec`` — handing that to every C-end user is
    an arbitrary-program-execution primitive on the host. stdio is therefore
    admin-only; regular users are limited to remote (``http``) transports.
    """


# Transports the MCP layer can actually open (see McpServerConfig.transport).
_ALLOWED_TRANSPORTS = frozenset({"stdio", "http", "sse"})


class ConnectorService:
    """Encrypted, tenant-scoped connector CRUD."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _owner_is_admin(self, user_id: uuid.UUID) -> bool:
        """Whether the connector's owner carries the admin role.

        Synchronous by design: callers sit inside async flows that already
        hold a session, and the check runs against the identity-scoped role
        column (no cache — a demoted admin must lose stdio immediately).
        """
        from sqlalchemy import select

        from app.models import User

        role = await self._db.scalar(select(User.role).where(User.id == user_id))
        return bool(role == "admin")

    # ------------------------------------------------------------------ #
    # Create
    # ------------------------------------------------------------------ #
    async def create(
        self,
        *,
        user_id: uuid.UUID,
        name: str,
        provider: str,
        credentials: dict[str, Any],
        oauth_scopes: list[str],
        command_or_url: str | None = None,
        transport: str | None = None,
        enabled: bool = False,
        extra: dict[str, Any] | None = None,
        is_admin: bool = False,
    ) -> Connector:
        """Create a connector for ``user_id`` with credentials encrypted.

        The provider must exist in the catalog; the manifest is snapshotted
        onto the row. ``command_or_url``/``transport`` default to the
        manifest's values. ``stdio`` transports require ``is_admin`` (the
        command would run as a server-side subprocess — see
        :class:`StdioConnectorForbiddenError`).
        """
        manifest = get_manifest(provider)
        if manifest is None:
            raise ValueError(f"unknown provider: {provider!r}")

        cmd = command_or_url or manifest.command_or_url
        txn = (transport or manifest.transport).lower()

        if txn == "stdio" and not is_admin:
            raise StdioConnectorForbiddenError(
                "stdio connectors execute a program on the server and are "
                "restricted to administrators; use a remote (http) connector"
            )
        if txn not in _ALLOWED_TRANSPORTS:
            raise ValueError(
                f"unsupported transport {txn!r} (allowed: {sorted(_ALLOWED_TRANSPORTS)})"
            )

        if enabled:
            self._check_scopes(oauth_scopes, manifest.required_scopes)
            # Connector quota gate (Task 11b): creating an enabled connector
            # consumes a slot, same as enable(). Gated on QUOTAS_ENABLED.
            await self._check_connector_quota(user_id)

        conn = Connector(
            user_id=user_id,
            name=name,
            provider=provider,
            manifest=manifest.to_dict(),
            transport=txn,
            command_or_url=cmd,
            credentials_enc=encrypt_secret(json.dumps(credentials, sort_keys=True)),
            oauth_scopes=list(oauth_scopes),
            enabled=enabled,
            extra=extra,
        )
        self._db.add(conn)
        await self._db.flush()
        # Record the enabled connector against the quota counter (best-effort).
        if enabled:
            from app.quotas import get_quota_service

            svc = get_quota_service()
            if svc.enabled:
                try:
                    await svc.record_connector(str(user_id))
                except Exception:
                    logger.debug("record_connector failed for %s", user_id, exc_info=True)
        return conn

    # ------------------------------------------------------------------ #
    # Read (tenant-scoped)
    # ------------------------------------------------------------------ #
    async def list_for_user(self, user_id: uuid.UUID) -> list[Connector]:
        res = await self._db.execute(
            select(Connector)
            .where(Connector.user_id == user_id)
            .order_by(Connector.created_at.desc())
        )
        return list(res.scalars().all())

    async def get_for_user(
        self, user_id: uuid.UUID, connector_id: uuid.UUID
    ) -> Connector | None:
        """Return the connector only if it belongs to ``user_id``."""
        res = await self._db.execute(
            select(Connector).where(
                Connector.id == connector_id,
                Connector.user_id == user_id,
            )
        )
        return res.scalar_one_or_none()

    async def _get_owned_or_raise(
        self, user_id: uuid.UUID, connector_id: uuid.UUID
    ) -> Connector:
        conn = await self.get_for_user(user_id, connector_id)
        if conn is None:
            raise ConnectorNotFoundError(
                f"connector {connector_id} not found for user {user_id}"
            )
        return conn

    # ------------------------------------------------------------------ #
    # Credentials (in-memory decrypt only)
    # ------------------------------------------------------------------ #
    def decrypted_credentials(self, connector: Connector) -> dict[str, Any]:
        """Return the plaintext credentials dict — IN MEMORY ONLY.

        The plaintext is never persisted; this helper is the sole path to it,
        and callers must not cache it. Returns ``{}`` when the row has no
        stored ciphertext (or decryption fails under key rotation).
        """
        if not connector.credentials_enc:
            return {}
        plaintext = decrypt_secret(connector.credentials_enc)
        if not plaintext:
            return {}
        try:
            parsed = json.loads(plaintext)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            # Legacy / raw string credential.
            return {"value": plaintext}

    async def rotate_credentials(
        self,
        user_id: uuid.UUID,
        connector_id: uuid.UUID,
        new_credentials: dict[str, Any],
    ) -> Connector:
        """Replace the stored ciphertext with a fresh encryption of the new creds."""
        conn = await self._get_owned_or_raise(user_id, connector_id)
        conn.credentials_enc = encrypt_secret(
            json.dumps(new_credentials, sort_keys=True)
        )
        await self._db.flush()
        return conn

    # ------------------------------------------------------------------ #
    # Enable / disable (minimum-scope gate on enable + connector quota)
    # ------------------------------------------------------------------ #
    async def _check_connector_quota(self, user_id: uuid.UUID) -> None:
        """Refuse if the tenant is at/over their connector cap (Task 11b).

        Gated on ``QUOTAS_ENABLED`` (the quota service is a no-op when disabled,
        which is the default + the test env), so existing tests are unaffected.
        Maps the admin-visible :class:`QuotaExceeded` to an ``AppException(429)``
        carrying the quota dict so the operator sees the reason + limit + usage.
        """
        from app.quotas import QuotaExceeded, get_quota_service

        svc = get_quota_service()
        if not svc.enabled:
            return
        try:
            await svc.check_connector(str(user_id))
        except QuotaExceeded as exc:
            raise AppException(
                429,
                "quota_exceeded",
                exc.reason,
                {"quota": exc.to_dict()},
            ) from exc

    async def enable(self, user_id: uuid.UUID, connector_id: uuid.UUID) -> Connector:
        # Observability (Task 11b): one span per enable op (redacted attrs).
        with observe_span("connector.enable", tenant=str(user_id)):
            conn = await self._get_owned_or_raise(user_id, connector_id)
            required = conn.manifest.get("required_scopes") or []
            self._check_scopes(conn.oauth_scopes or [], required)
            # Quota gate: only when flipping disabled→enabled (idempotent enable
            # of an already-enabled connector is a no-op and must not double-count).
            was_enabled = bool(conn.enabled)
            if not was_enabled:
                await self._check_connector_quota(user_id)
            conn.enabled = True
            await self._db.flush()
        # Best-effort: record the newly-enabled connector against the quota
        # counter so subsequent enables see an accurate count. Only on the
        # disabled→enabled transition.
        if not was_enabled:
            from app.quotas import get_quota_service

            svc = get_quota_service()
            if svc.enabled:
                try:
                    await svc.record_connector(str(user_id))
                except Exception:
                    logger.debug("record_connector failed for %s", user_id, exc_info=True)
        observe_counter("connector.enables", 1, outcome="ok")
        return conn

    async def disable(self, user_id: uuid.UUID, connector_id: uuid.UUID) -> Connector:
        with observe_span("connector.disable", tenant=str(user_id)):
            conn = await self._get_owned_or_raise(user_id, connector_id)
            conn.enabled = False
            await self._db.flush()
        observe_counter("connector.enables", 1, outcome="disabled")
        return conn

    async def delete(self, user_id: uuid.UUID, connector_id: uuid.UUID) -> None:
        """Delete a connector. Idempotent: a miss (wrong tenant) is a no-op."""
        conn = await self.get_for_user(user_id, connector_id)
        if conn is not None:
            await self._db.delete(conn)
            await self._db.flush()

    # ------------------------------------------------------------------ #
    # Update (mutable non-credential fields; scope change re-validates)
    # ------------------------------------------------------------------ #
    async def update(
        self,
        user_id: uuid.UUID,
        connector_id: uuid.UUID,
        *,
        name: str | None = None,
        oauth_scopes: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Connector:
        """Update mutable fields. When ``oauth_scopes`` is changed AND the
        connector is currently enabled, the minimum-scope gate is re-run: if
        the new scopes no longer cover the manifest's required scopes, the
        connector is auto-disabled (rather than silently left enabled with
        insufficient scopes). Returns the updated row.
        """
        conn = await self._get_owned_or_raise(user_id, connector_id)
        if name is not None:
            conn.name = name
        if oauth_scopes is not None:
            conn.oauth_scopes = list(oauth_scopes)
            if conn.enabled:
                required = conn.manifest.get("required_scopes") or []
                missing = [
                    s for s in required if s not in set(conn.oauth_scopes or [])
                ]
                if missing:
                    # M4: never leave an enabled connector below the scope floor.
                    conn.enabled = False
                    logger.info(
                        "connector %s auto-disabled: scopes dropped below minimum (missing %s)",
                        conn.id, missing,
                    )
        if extra is not None:
            conn.extra = extra
        await self._db.flush()
        return conn

    # ------------------------------------------------------------------ #
    # Connector → McpServerConfig seam (per-tenant session wiring hook)
    # ------------------------------------------------------------------ #
    async def build_server_config(self, connector: Connector) -> Any:
        """Materialize an :class:`McpServerConfig` for this connector row.

        The decrypted credentials are placed in the ``env`` under
        ``MCP_CREDENTIALS`` (a JSON string) plus flattened top-level, so an MCP
        server subprocess can read them via env. This is the documented seam
        for per-tenant connector→session wiring (a follow-up): a session
        manager would call this, hand the result to :class:`McpSession`, and
        register the discovered tools into that user's run-scoped registry via
        :func:`merge_mcp_tools`.

        Defense in depth: a ``stdio`` connector owned by a non-admin is
        refused here too (not just at create time) — the config build is the
        last stop before ``create_subprocess_exec``, so a stdio row that
        predates the create-time gate or was flipped by direct DB access
        cannot spawn a process.
        """
        from app.agents.mcp_client import McpServerConfig  # local: avoid import cycle

        if (connector.transport or "").lower() == "stdio" and not await self._owner_is_admin(
            connector.user_id
        ):
            raise StdioConnectorForbiddenError(
                f"stdio connector {connector.id} is admin-only; skipping"
            )

        creds = self.decrypted_credentials(connector)
        env: dict[str, str] = {"MCP_CREDENTIALS": json.dumps(creds, sort_keys=True)}
        # Flatten top-level string credentials into env too (common case: a
        # single ``access_token`` / ``api_key``).
        for k, v in creds.items():
            if isinstance(v, str):
                env[str(k).upper()] = v
        return McpServerConfig(
            name=f"{connector.provider}:{connector.id}",
            command=connector.command_or_url,
            args=[],
            env=env,
            transport=connector.transport,
        )

    # ------------------------------------------------------------------ #
    # Scope validation
    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_scopes(
        granted: list[str], required: list[str]
    ) -> None:
        granted_set = set(granted or [])
        missing = [s for s in (required or []) if s not in granted_set]
        if missing:
            raise InsufficientScopesError(missing)
