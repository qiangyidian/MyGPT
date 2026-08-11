"""Artifact ORM model (Task 10).

A first-class, authorized reference to a blob produced or uploaded by the
platform: a tool output, a code bundle, a screenshot, an audio clip, an image,
a generated document — or a user upload. Distinct from
:class:`~app.models.chat_attachment.ChatAttachment` (a per-conversation upload
bound to a message) and from KB :class:`~app.models.document.Document` (a
long-term RAG-indexed source).

Security model:
  * **Tenant-scoped by owner.** Every query filters on ``owner_id``; a foreign
    user cannot resolve, list, or download another tenant's artifact. The API
    returns 404 (not 403) for a foreign id so existence never leaks.
  * **Opaque storage key.** ``storage_key`` is the only stored reference to the
    bytes; it is never handed to the model or the client. The model sees
    ``artifact:<id>``; the backend resolves id → owner check → StorageBackend.
  * **Checksum + size + media_type** are computed at create time and persisted;
    reads re-verify the sha256 to detect corruption/tamper.
  * **Retention.** ``expires_at`` (nullable) hides an expired artifact from
    listing/download; ``retention_policy`` is the human/audit reason.

Provenance: ``source`` (tool_output | spill | upload | generation) and
``generator`` (free-form: tool name, run/step ids, model) attribute the
artifact so the frontend and audit trail can render its origin.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

# Allowed provenance sources.
SOURCES = {"tool_output", "spill", "upload", "generation"}


class Artifact(Base, TimestampMixin):
    """A first-class, tenant-scoped, checksummed blob reference."""

    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Tenant scope — every query filters on this column.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Optional links into the agent platform for attribution.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # sha256 hex of the stored bytes; verified on every read.
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(
        String(255), nullable=False, default="application/octet-stream"
    )
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Opaque storage backend key (path for LocalStorage, object key for MinIO).
    # NEVER exposed to the model or client — only the artifact id is public.
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)

    # Display name (sanitized basename) for downloads / Content-Disposition.
    filename: Mapped[str] = mapped_column(String(255), nullable=False, default="artifact")

    # Provenance.
    #   tool_output | spill | upload | generation
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="upload")
    # Free-form generator info: {tool, run_id, step_id, model, ...}.
    generator: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Retention: an expired artifact is hidden from open/listing and may be
    # reaped. ``retention_policy`` is the audit reason (e.g. "delete_after_run").
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retention_policy: Mapped[str | None] = mapped_column(String(64), nullable=True)
