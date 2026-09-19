from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import ORMModel


class RetrievalSettings(BaseModel):
    """Per-KB retrieval / chunking overrides.

    ``None`` is meaningful: it says "inherit the platform default", not "turn
    the feature off". That is why every field is optional here *and* nullable in
    the column — a KB created before this feature must keep following whatever
    the operator sets globally, instead of freezing the defaults of the day it
    was created.
    """

    top_k: int | None = Field(default=None, ge=1, le=50)
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    # 重排是跨库一次的动作，所以这个开关的含义是「允许重排」：一次请求里任一库
    # 打开即启用，全部显式关闭才跳过（见 app/rag/rag_service.py）。
    rerank_enabled: bool | None = None
    # Only affects documents indexed after the change.
    chunk_size: int | None = Field(default=None, ge=50, le=8000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=4000)

    @model_validator(mode="after")
    def _overlap_below_size(self) -> RetrievalSettings:
        if (
            self.chunk_size is not None
            and self.chunk_overlap is not None
            and self.chunk_overlap >= self.chunk_size
        ):
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        return self


class KnowledgeBaseCreate(RetrievalSettings):
    name: str
    description: str | None = None
    embedding_model_id: uuid.UUID | None = None


class KnowledgeBaseUpdate(RetrievalSettings):
    """PATCH body: omitted fields are untouched, explicit nulls reset to inherit."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    embedding_model_id: uuid.UUID | None = None


class KnowledgeBaseOut(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    description: str | None
    embedding_model_id: uuid.UUID | None
    top_k: int | None = None
    score_threshold: float | None = None
    rerank_enabled: bool | None = None
    chunk_size: int | None = None
    chunk_overlap: int | None = None
    document_count: int = 0
    chunk_count: int = 0
    created_at: datetime
