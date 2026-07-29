"""Conversation memory schemas (Phase 3 — user memory management)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class MemoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    conversation_id: uuid.UUID
    memory_type: str
    content: str
    structured_value: Optional[dict[str, Any]] = None
    confidence: float
    confirmed_by_user: bool
    expires_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class MemoryUpdate(BaseModel):
    content: Optional[str] = None
    confirmed_by_user: Optional[bool] = None
