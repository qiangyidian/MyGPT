from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class RunLease(Base, TimestampMixin):
    """The live execution lease for a run (one per run at a time).

    A worker acquires a lease to claim ownership of a run's execution; the
    monotonic ``version`` supports optimistic fencing (Task 5 will use this for
    safe handoff / takeover). ``run_id`` is unique so only one live lease exists
    per run; ``expires_at`` lets a stale lease be reclaimed.
    """

    __tablename__ = "run_leases"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_run_leases_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
