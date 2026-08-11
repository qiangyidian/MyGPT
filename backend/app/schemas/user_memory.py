"""User-level semantic memory schemas (Task 7 — opt-in long-term memory).

Distinct from the conversation-scoped :class:`MemoryOut`/:class:`MemoryUpdate`
in :mod:`app.schemas.memory`. These cover activate/deactivate/edit/delete on a
user's cross-conversation semantic memory.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class UserMemoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    memory_type: str
    content: str
    structured_value: Optional[dict[str, Any]] = None
    confidence: float
    active: bool
    confirmed_by_user: bool
    source_message_id: Optional[uuid.UUID] = None
    source_conversation_id: Optional[uuid.UUID] = None
    expires_at: Optional[datetime] = None
    embedding_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class UserMemoryPropose(BaseModel):
    """Body for proposing a new candidate memory (inactive by default)."""

    content: str
    memory_type: str = "fact"
    confidence: float = 0.5
    source_message_id: Optional[uuid.UUID] = None
    source_conversation_id: Optional[uuid.UUID] = None


class UserMemoryEdit(BaseModel):
    """Body for editing a memory's content (re-embeds if active)."""

    content: str


class UserMemoryBulkAction(BaseModel):
    """Body for bulk activate/deactivate on the user's memories."""

    active: bool
