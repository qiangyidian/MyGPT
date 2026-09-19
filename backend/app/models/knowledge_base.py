from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin


class KnowledgeBase(Base, TimestampMixin):
    __tablename__ = "knowledge_bases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_configs.id", ondelete="SET NULL"), nullable=True
    )
    qdrant_collection: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # ---- 每库检索 / 切分参数 ------------------------------------------------
    # 一律可空，NULL = 沿用全局默认（``settings.RAG_*``）。把默认值写进列定义
    # 等于把「改一次全局默认」变成一次数据迁移，而且既有行会凭空得到一套与运维
    # 配置无关的策略。
    top_k: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 重排是全局一次的动作（跨库融合后统一打分），所以这个开关是「是否允许重排」
    # 而不是「只重排本库」：本次请求里任一库显式打开即启用，全部显式关闭才跳过。
    rerank_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 只影响之后的重新索引：已入库的向量是按旧切分算的。
    chunk_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_overlap: Mapped[int | None] = mapped_column(Integer, nullable=True)

    documents = relationship(
        "Document",
        back_populates="knowledge_base",
        cascade="all, delete-orphan",
        # ``lazy="raise"`` — the old ``selectin`` made *every* KB read (the list
        # endpoint, each chat turn's ownership check, retrieval, ``db.get`` in
        # the upload guard) select the KB's full document rows, and each of
        # those rows then dragged its own chunks along. Counts are aggregated
        # explicitly instead (app/api/knowledge_bases.py::_counts); deleting a
        # KB deletes its documents explicitly.
        lazy="raise",
    )
