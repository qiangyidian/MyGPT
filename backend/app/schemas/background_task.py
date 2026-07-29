"""Background task schemas (Phase 3)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class BackgroundTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: str
    status: str
    payload: dict[str, Any]
    result: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    conversation_id: Optional[uuid.UUID] = None
    scheduled_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class BackgroundTaskEnqueue(BaseModel):
    kind: str
    payload: dict[str, Any] = {}
    conversation_id: Optional[uuid.UUID] = None
    scheduled_at: Optional[datetime] = None
