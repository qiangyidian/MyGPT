"""Connector ORM model (Task 9).

A ``Connector`` is a tenant-scoped definition of an MCP server connection:
which provider (catalog manifest snapshot), how to reach it
(transport + command/URL), the encrypted credentials to authenticate, the
OAuth scopes the tenant granted, and an enable/disable flag. Credentials are
stored ONLY as a Fernet ciphertext (``credentials_enc``); the plaintext is
materialized in memory for the duration of an active session via
:meth:`ConnectorService.decrypted_credentials` and never persisted.

Tenant isolation: every query is scoped by ``user_id``; one tenant's
connector is invisible and unusable by another. A connector is registered
with the MCP tool gateway only when ``enabled`` is true.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, false, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# NOTE: this model deliberately inlines the timestamp columns rather than
# importing ``TimestampMixin`` from ``app.models._mixins``. Importing a
# submodule of ``app.models`` runs ``app.models.__init__``, which re-exports
# ``Connector`` — a circular import that fails at app boot. Inlining keeps
# this module dependent only on ``app.db.Base``.


class Connector(Base):
    """An encrypted, tenant-scoped MCP connector definition."""

    __tablename__ = "connectors"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Tenant scope. Every query filters on this column.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Catalog key (e.g. "github", "slack"). Must exist in PROVIDER_CATALOG.
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Snapshot of the catalog manifest at create time — stable even if the
    # catalog is later edited.
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # "stdio" | "http"
    transport: Mapped[str] = mapped_column(String(16), nullable=False, default="stdio")
    # Subprocess command (stdio) OR server URL (http).
    command_or_url: Mapped[str] = mapped_column(String(512), nullable=False)

    # Fernet ciphertext of the credentials JSON. NEVER store plaintext.
    credentials_enc: Mapped[str] = mapped_column(String(4096), nullable=False, default="")
    # OAuth scopes the tenant granted (must be a superset of the manifest's
    # required_scopes to enable).
    oauth_scopes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    # Optional provider-specific config (e.g. default channel, repo).
    extra: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Timestamps inlined (see module note about avoiding a circular import).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
