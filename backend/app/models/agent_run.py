from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
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
    # List endpoint sorts by created_at DESC (admin sees the whole table).
    __table_args__ = (
        Index("ix_agent_runs_created_at", "created_at"),
    )

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
    # pending | running | waiting_approval | paused | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    current_step: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    input: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    model_config_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ---- multi-agent graph (Phase: multi-agent visualization) ----
    # Static topology: nodes + edges + mode (written once at run start).
    graph_definition: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Live state snapshot: the same graph with updated node/edge statuses,
    # active_agent_ids, run status, timing. Updated as agents progress so a
    # GET /api/agent-runs/{id} after a refresh restores the exact picture.
    graph_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # ---- Phase 1+: research plan + run-time instructions (reserved) ----
    # Draft research plan for deep_research mode (steps, summary, sources).
    plan: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # draft | confirmed | rejected | updated
    plan_status: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    # Free-form mid-run guidance the user appends while a run is in flight.
    user_instructions: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Explicit user-driven pause (distinct from waiting_approval).
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Opaque token/version for safe resume (reserved).
    resume_token: Mapped[str] = mapped_column(String(64), default="", nullable=False)
