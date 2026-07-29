"""Background tasks: durable, resumable units of work (Phase 3).

A lightweight task queue on top of the DB + an inprocess asyncio worker (the
configured ``BACKGROUND_WORKER``). Each row tracks kind/status/payload/result
and is user-scoped. Kinds are registered with handlers in
``app.services.background_task_service``. Scheduled/cron tasks are reserved
(scheduled_at populated, a dispatcher can pick them up); this table also
serves as the audit trail.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class BackgroundTask(Base, TimestampMixin):
    __tablename__ = "background_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    # e.g. document_index | conversation_summarize | attachment_parse | custom
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # pending | running | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
