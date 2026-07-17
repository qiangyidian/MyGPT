from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import ORMModel


class DocumentOut(ORMModel):
    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    filename: str
    file_type: str
    file_size: int
    status: str            # pending|parsing|chunking|embedding|indexed|failed
    error_message: str | None
    chunk_count: int
    created_at: datetime
    updated_at: datetime


class ReindexResult(BaseModel):
    document_id: uuid.UUID
    status: str
    chunk_count: int = 0
