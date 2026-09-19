from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin


class DocumentChunk(Base, TimestampMixin):
    """A text chunk. The embedding vector itself lives in Qdrant (point id == chunk id);
    this row holds the text + metadata for citation rendering and reindex.
    """
    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)

    # ---- 向量溯源 -----------------------------------------------------------
    # 向量本身在 Qdrant（point id == chunk id），这里存的是「这枚向量是谁算的」。
    # 没有这两列，换 embedding 模型就变成一次不可见的破坏：旧点留在同一个
    # collection 里参与检索，维度和语义空间都不对，但库里没有任何地方能查出是哪
    # 一批块坏的 —— 只能整库重建。content_sha256 则让「重建时跳过内容没变的块」
    # 和「检测内容被改过却没重新索引」成为可能。
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    document = relationship("Document", back_populates="chunks")
