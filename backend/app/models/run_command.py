from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class RunCommand(Base, TimestampMixin):
    """A durable control command issued against a run.

    Replaces the in-memory pause/resume/cancel/instruction surface with a
    persisted, exactly-once command queue. The API layer PERSISTS a row here
    FIRST, then publishes the live wake-up signal (approval_bus / run_controls).
    A worker claims pending commands to apply them, moving
    ``pending -> claimed -> applied`` (or ``failed``). Rows are never deleted,
    so the full command history stays auditable.
    """

    __tablename__ = "run_commands"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # pause | resume | cancel | instruction | approve | reject
    command_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # pending | claimed | applied | failed
    status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False, index=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
