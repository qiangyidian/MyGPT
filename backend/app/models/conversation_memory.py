from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class ConversationMemory(Base, TimestampMixin):
    """Long-lived, per-conversation memory entries (facts, preferences, summaries, tasks).

    Always scoped by ``user_id`` + ``conversation_id`` so memories never cross
    tenants. ``confirmed_by_user`` separates facts the user affirmed from
    inferences the model made — only confirmed facts should be treated as ground
    truth. ``expires_at`` lets time-boxed facts (e.g. "user is on vacation") age out.
    """

    __tablename__ = "conversation_memories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
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
    # fact | preference | summary | task
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    structured_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    confirmed_by_user: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ---- 工作记忆块（spec §6.1）----
    # 历史行（task / summary）由 0022 回填；其余行为 NULL（Postgres 的 NULL 互不
    # 相等，所以旧行不会互撞唯一键）。
    block_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    author: Mapped[str] = mapped_column(String(16), default="system", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    __table_args__ = (
        UniqueConstraint("conversation_id", "block_key", name="uq_conversation_memories_block"),
    )
