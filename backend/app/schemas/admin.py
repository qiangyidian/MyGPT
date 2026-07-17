from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class AdminUserUpdate(BaseModel):
    role: str | None = None
    is_active: bool | None = None


class UsageStat(BaseModel):
    date: str
    conversations: int = 0
    messages: int = 0
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0


class SystemStatus(BaseModel):
    db: str = "unknown"
    redis: str = "unknown"
    qdrant: str = "unknown"
    users: int = 0
    conversations: int = 0
    documents: int = 0
    uptime_s: float = 0.0


class AuditLogOut(BaseModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    target: str | None
    detail: dict | None = None
    created_at: datetime
