from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import ORMModel


class KnowledgeBaseCreate(BaseModel):
    name: str
    description: str | None = None
    embedding_model_id: uuid.UUID | None = None


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    embedding_model_id: uuid.UUID | None = None


class KnowledgeBaseOut(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    description: str | None
    embedding_model_id: uuid.UUID | None
    document_count: int = 0
    chunk_count: int = 0
    created_at: datetime
