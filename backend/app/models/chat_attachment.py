"""Per-conversation / per-message chat attachments.

Distinct from long-lived KnowledgeBase documents (``app.models.document``):

  * KB documents are long-term, RAG-indexed, cross-conversation.
  * Chat attachments are bound to a conversation (and optionally a message),
    default to the current session only, are not auto-indexed into the shared
    KB, and the user may opt to "save to KB" later.

The file bytes live in the storage backend (``app.core.storage``); only the
metadata row is persisted here. Status is the user-facing lifecycle of the
attachment as a whole; ``parse_status`` tracks background text extraction.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class ChatAttachment(Base, TimestampMixin):
    __tablename__ = "chat_attachments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Null until the attachment is bound to a specific user message on send.
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    # Storage key returned by the storage backend (opaque to the frontend).
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)

    # uploading | uploaded | parsing | ready | failed | deleted
    status: Mapped[str] = mapped_column(String(32), default="uploading", nullable=False)
    # pending | parsing | ready | failed. Images are OCR'd (multimodal fallback
    # text); a vision model still receives the raw bytes at send time.
    parse_status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False
    )
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Structured preview hints (page count, sheet names, row/col count, dims...).
    preview_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_temporary: Mapped[bool] = mapped_column(default=True, nullable=False)
    # Set when the user promotes an attachment into the knowledge base.
    knowledge_base_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
