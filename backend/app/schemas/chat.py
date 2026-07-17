from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel


class ChatRole(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


class ChatMessage(BaseModel):
    """OpenAI-style message used to talk to providers."""
    role: ChatRole
    content: str | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class Citation(BaseModel):
    document_id: uuid.UUID
    document_name: str
    chunk_id: uuid.UUID | None = None
    chunk_index: int = 0
    snippet: str = ""
    score: float = 0.0


class ChatRequest(BaseModel):
    """POST /api/chat/stream body. conversation_id optional for ad-hoc chat."""
    conversation_id: uuid.UUID | None = None
    model_id: uuid.UUID | None = None
    knowledge_base_id: uuid.UUID | None = None
    content: str = ""
    regenerate: bool = False          # regenerate last assistant turn
    stream: bool = True
    enable_tools: bool = False        # P2 toggle


# ---- SSE event payloads ----------------------------------------------------
class MetaEvent(BaseModel):
    message_id: str
    conversation_id: str


class TokenEvent(BaseModel):
    delta: str


class CitationEvent(BaseModel):
    citations: list[Citation]


class ToolCallEvent(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class ToolResultEvent(BaseModel):
    id: str
    name: str
    ok: bool
    result: Any = None
    error: str | None = None


class DoneEvent(BaseModel):
    message_id: str
    finish_reason: str = "stop"


class ErrorEvent(BaseModel):
    code: str
    message: str
