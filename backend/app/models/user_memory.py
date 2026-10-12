"""User-level semantic long-term memory (Task 7).

Distinct from :class:`ConversationMemory` (which is conversation-scoped and has
a NOT-NULL ``conversation_id``), a ``UserMemory`` is a USER-wide semantic fact
that crosses conversations: "prefers concise answers", "uses Python", … It is
opt-in (``active=False`` until the user activates it), tenant-scoped by
``user_id`` (never crosses users), and carries provenance
(``source_message_id`` / ``source_conversation_id``) plus confidence and
expiry — reusing the consent/provenance/confidence vocabulary of
ConversationMemory.

``embedding_id`` is the opaque id of this memory's vector in the user-scoped
Qdrant collection, written when the memory is activated. Inactive memories
have no embedding and are never retrieved into the effective prompt.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class UserMemory(Base, TimestampMixin):
    """Opt-in, user-scoped semantic memory (Task 7 long-term memory)."""

    __tablename__ = "user_memories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # fact | preference | summary | instruction | …
    memory_type: Mapped[str] = mapped_column(String(32), default="fact", nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    structured_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)

    # Provenance: which message / conversation surfaced this candidate.
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Consent gate. ``active=False`` until the user opts the candidate in —
    # inactive rows are never embedded and never reach the effective prompt.
    active: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    confirmed_by_user: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Opaque id of this memory's vector in the user-scoped collection. Set on
    # activate / edit; cleared on deactivate / delete.
    embedding_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # ---- 三层记忆（spec §5.1）----
    # 谁写的：显式"记住" / 抽取器推断 / 用户在设置页手写。决定写入后是否立即 active。
    source: Mapped[str] = mapped_column(
        String(16), default="inferred", nullable=False, index=True
    )
    author: Mapped[str] = mapped_column(String(16), default="extractor", nullable=False)
    # "这值得记住"（confidence 是"我确定这是真的"）—— 两者都进排序，含义不同。
    importance: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    # 双时态：事实在世界上的成立区间。冲突时失效而非删除（spec §5.4）。
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    invalid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_memories.id", ondelete="SET NULL"),
        nullable=True,
    )
    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_user_memories_user_invalid", "user_id", "invalid_at"),
        Index("ix_user_memories_user_superseded", "user_id", "superseded_by"),
    )
