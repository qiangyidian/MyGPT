from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class ChatAttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: Optional[uuid.UUID] = None
    filename: str
    original_filename: str
    mime_type: str
    size_bytes: int
    status: str
    parse_status: str
    preview_metadata: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    is_temporary: bool
    knowledge_base_id: Optional[uuid.UUID] = None
    created_at: datetime
    updated_at: datetime


class SaveToKbRequest(BaseModel):
    knowledge_base_id: uuid.UUID
