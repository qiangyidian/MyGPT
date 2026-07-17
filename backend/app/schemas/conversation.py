from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import ORMModel
from app.schemas.message import MessageOut


class ConversationCreate(BaseModel):
    title: str | None = None
    model_id: uuid.UUID | None = None
    knowledge_base_id: uuid.UUID | None = None
    system_prompt: str | None = None


class ConversationUpdate(BaseModel):
    title: str | None = None
    model_id: uuid.UUID | None = None
    knowledge_base_id: uuid.UUID | None = None
    system_prompt: str | None = None


class ConversationOut(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    model_id: uuid.UUID | None
    knowledge_base_id: uuid.UUID | None
    system_prompt: str | None
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut] = []
