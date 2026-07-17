from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.schemas.common import ORMModel


class ToolParameter(BaseModel):
    name: str
    type: str
    description: str
    required: bool = True
    default: Any = None
    enum: list[str] | None = None


class ToolInfo(BaseModel):
    name: str
    description: str
    parameters: list[ToolParameter]
    category: str = "general"
    dangerous: bool = False        # e.g. code execution


class ToolTestRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = {}


class ToolTestResult(BaseModel):
    ok: bool
    result: Any = None
    error: str | None = None
    latency_ms: int = 0


class ToolCallOut(ORMModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID | None
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None
    status: str
    error_message: str | None
    created_at: datetime
