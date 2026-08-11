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
    ``required_scopes``.

The service does NOT itself open MCP sessions; it owns definitions. Wiring a
definition into the live MCP tool gateway happens in the connector router /
agent startup (Task 9 step 3).
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
from app.core.security import encrypt_secret, decrypt_secret

logger = logging.getLogger(__name__)


class InsufficientScopesError(Exception):
    """Raised when enabling a connector whose scopes miss a required scope."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f"missing required OAuth scopes: {', '.join(missing)}")


class ConnectorNotFoundError(Exception):
    """Raised when a tenant-scoped lookup misses (no row OR wrong tenant)."""


class ConnectorService:
    """Encrypted, tenant-scoped connector CRUD."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

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
    ) -> Connector:
        """Create a connector for ``user_id`` with credentials encrypted.

        The provider must exist in the catalog; the manifest is snapshotted
        onto the row. ``command_or_url``/``transport`` default to the
        manifest's values.
        """
        manifest = get_manifest(provider)
        if manifest is None:
            raise ValueError(f"unknown provider: {provider!r}")

        cmd = command_or_url or manifest.command_or_url
        txn = (transport or manifest.transport).lower()

        if enabled:
            self._check_scopes(oauth_scopes, manifest.required_scopes)

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
    # Enable / disable (minimum-scope gate on enable)
    # ------------------------------------------------------------------ #
    async def enable(self, user_id: uuid.UUID, connector_id: uuid.UUID) -> Connector:
        conn = await self._get_owned_or_raise(user_id, connector_id)
        required = conn.manifest.get("required_scopes") or []
        self._check_scopes(conn.oauth_scopes or [], required)
        conn.enabled = True
        await self._db.flush()
        return conn

    async def disable(self, user_id: uuid.UUID, connector_id: uuid.UUID) -> Connector:
        conn = await self._get_owned_or_raise(user_id, connector_id)
        conn.enabled = False
        await self._db.flush()
        return conn

    async def delete(self, user_id: uuid.UUID, connector_id: uuid.UUID) -> None:
        """Delete a connector. Idempotent: a miss (wrong tenant) is a no-op."""
        conn = await self.get_for_user(user_id, connector_id)
        if conn is not None:
            await self._db.delete(conn)
            await self._db.flush()

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
