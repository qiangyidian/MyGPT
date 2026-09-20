from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin


class Document(Base, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_type: Mapped[str] = mapped_column(String(32), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Indexed: the queue's claim query filters on it on every poll.
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    # pending | parsing | chunking | embedding | indexed | failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ---- 摄取队列（这一行就是任务本身，所以不再另建队列表）----------------
    # 没有这几列时，索引跑在「谁收到上传谁执行」的进程内后台任务里：一次发版
    # 或崩溃就把任务销毁，文档永远停在 pending/parsing，而且坏文档会被无限重试。
    # lease + claimed_by 让「接管」与「误接管」可以区分：租约到期前别人不能碰，
    # 到期后任何进程都能接手，而接手之后旧进程再写回会被 claimed_by 挡掉。
    ingest_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    ingest_next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ingest_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ingest_claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    knowledge_base = relationship("KnowledgeBase", back_populates="documents")
    chunks = relationship(
        "DocumentChunk",
        back_populates="document",
        cascade="all, delete-orphan",
        # ``lazy="raise"``: an implicit load here selected EVERY chunk body of a
        # document (hundreds to tens of thousands of Text rows) on any
        # ``select(Document)`` / ``db.get(Document)`` — the document list, the
        # keyword retriever's join, file_analyze, each ingestion run. No caller
        # reads this collection (chunk text is queried explicitly where needed,
        # e.g. app/tools/builtin.py), so accessing it is now a hard error rather
        # than a silent full-table read. Deleting a document therefore has to
        # delete its chunks explicitly (app/services/document_service.py).
        lazy="raise",
    )
