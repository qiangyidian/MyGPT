from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, Text
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
