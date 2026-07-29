"""Audit event log (Phase 3): an append-only trail of security-relevant actions.

Surfaced via GET /api/admin/audit for compliance/procurement (SOC 2, ISO 27001,
GDPR Art. 30). Emitted from tool execution, dangerous-tool approvals, and auth.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # e.g. "tool_call", "approval:approved", "auth:login"
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # e.g. "web_search", "approval:<id>", "user:<id>"
    target: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
