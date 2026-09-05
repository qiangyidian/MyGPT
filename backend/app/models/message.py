from __future__ import annotations

import uuid
from datetime import datetime, UTC

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, String, Text
from sqlalchemy import Index as _sa_Index
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin


class Message(Base, TimestampMixin):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    __table_args__ = (
        _sa_Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )
    # Override the mixin's created_at with a PER-ROW Python default. The mixin
    # uses server_default=func.now(), which on Postgres resolves to the
    # TRANSACTION start time — identical for every row inserted in one
    # transaction. A chat turn persists the user message + the assistant
    # placeholder in a single commit (ChatService._run), so they shared one
    # created_at and ORDER BY created_at was non-deterministic (the assistant
    # reply could sort ahead of the user's question). A per-row Python
    # timestamp (microsecond precision) is distinct and keeps chronological
    # order stable.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)  # system | user | assistant | tool
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Per-message token/cost accounting. Populated for assistant messages from
    # the provider usage payload (the provider already parses usage; it used to
    # be discarded). Lets ops answer "who spent what" and enforce budgets.
    prompt_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    conversation = relationship(
        "Conversation",
        back_populates="messages",
        # Disambiguate from conversations.branch_from_message_id (a second
        # messages<->conversations FK path): this scalar uses conversation_id.
        foreign_keys="Message.conversation_id",
    )
    tool_calls = relationship(
        "ToolCall", back_populates="message", cascade="all, delete-orphan", lazy="selectin"
    )
