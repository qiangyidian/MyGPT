from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class MessageFeedbackRequest(BaseModel):
    """POST /api/messages/{message_id}/feedback."""
    rating: Literal["up", "down"]
    reason: Optional[str] = Field(default=None, max_length=64)
    comment: Optional[str] = Field(default=None, max_length=2000)


class MessageFeedbackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    rating: str
    reason: Optional[str] = None
    comment: Optional[str] = None
    created_at: datetime
    updated_at: datetime
