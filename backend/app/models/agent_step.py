from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class AgentStep(Base, TimestampMixin):
    """One granular step inside an :class:`AgentRun`.

    Step types: ``plan`` | ``llm`` | ``tool`` | ``review`` | ``approval``.
    Statuses: ``pending`` | ``running`` | ``waiting`` | ``done`` | ``error``.
    Inputs/outputs are stored *redacted* (secrets stripped, payloads truncated)
    so the audit trail is safe to surface in admin UIs.
    """

    __tablename__ = "agent_steps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step_type: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    # ---- multi-agent attribution (Phase: multi-agent visualization) ----
    # Stable graph node id this step belongs to (e.g. "researcher"), and the
    # CrewAI task id when available. Never hidden inside input_redacted.
    agent_id: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    parent_step_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    tool_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    input_redacted: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output_redacted: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Extra structured metadata (tool args preview, edge id, etc.) — never
    # secrets; the gateway redacts before persisting.
    step_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
