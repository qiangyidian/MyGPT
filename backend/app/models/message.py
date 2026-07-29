from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, Text
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
    role: Mapped[str] = mapped_column(String(32), nullable=False)  # system | user | assistant | tool
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

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
