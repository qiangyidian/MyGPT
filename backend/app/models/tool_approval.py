from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class ToolApproval(Base, TimestampMixin):
    """A human-in-the-loop gate for a dangerous tool call.

    Created ``pending`` when an agent wants to run a dangerous tool. The user
    approves/rejects via the agent-runs API; on approve the agent run resumes
    from the paused step. ``arguments_hash`` lets a user pre-approve an exact
    (tool, arguments) pair so repeat identical calls don't re-prompt. Expired
    approvals are treated as rejected.
    """

    __tablename__ = "tool_approvals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # low | medium | high
    risk_level: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    # pending | approved | rejected | expired
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
