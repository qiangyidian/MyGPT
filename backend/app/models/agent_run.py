from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class AgentRun(Base, TimestampMixin):
    """One agent execution against a single user turn.

    Tracks which runtime ran (native vs crewai), the flow name, lifecycle
    status (including ``waiting_approval`` for human-in-the-loop pauses),
    the model config snapshot at kickoff time, and timing. Steps for the run
    live in ``AgentStep``; dangerous-tool gates live in ``ToolApproval``.
    """

    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # native | crewai
    runtime: Mapped[str] = mapped_column(String(32), default="native", nullable=False)
    flow_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    # pending | running | waiting_approval | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    current_step: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    input: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    model_config_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
