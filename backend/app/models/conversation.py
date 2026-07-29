from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), default="新对话", nullable=False)
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_configs.id", ondelete="SET NULL"), nullable=True
    )
    knowledge_base_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="SET NULL"), nullable=True
    )
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Phase 1: conversation management ----
    # Pinned conversations float to the top of the sidebar.
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Archived conversations are hidden from the default list but recoverable.
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Cheap preview for the sidebar without loading the full message history.
    last_message_preview: Mapped[str | None] = mapped_column(String(280), nullable=True)
    # Branching: a conversation edited from an earlier message links back here.
    parent_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    branch_from_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Reserved for the Projects feature (Phase 3); no FK until that lands.
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)

    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
        lazy="selectin",
        # Disambiguate from branch_from_message_id (also a conversations<->messages
        # FK path): this collection is keyed by Message.conversation_id.
        foreign_keys="Message.conversation_id",
    )
    # Self-reference for conversation branches.
    parent = relationship(
        "Conversation", remote_side="Conversation.id", foreign_keys=[parent_conversation_id]
    )
