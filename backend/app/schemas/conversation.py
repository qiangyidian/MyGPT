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
    """PATCH /api/conversations/{id}. All fields optional."""
    title: str | None = None
    model_id: uuid.UUID | None = None
    knowledge_base_id: uuid.UUID | None = None
    system_prompt: str | None = None
    pinned: bool | None = None
    archived: bool | None = None


class ConversationOut(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    model_id: uuid.UUID | None
    knowledge_base_id: uuid.UUID | None
    system_prompt: str | None
    # ---- Phase 1 ----
    is_pinned: bool = False
    is_archived: bool = False
    last_message_preview: str | None = None
    parent_conversation_id: uuid.UUID | None = None
    branch_from_message_id: uuid.UUID | None = None
    # Soft reference to a Project (Phase 3); null = unfiled. Surfaced so the
    # sidebar can group conversations by project.
    project_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut] = []


class ConversationListResponse(BaseModel):
    """Paginated conversation list envelope (Phase 1)."""
    items: list[ConversationOut]
    next_cursor: str | None = None
    total: int | None = None


class ConversationBranchRequest(BaseModel):
    """Edit-and-resend from an earlier user message: create a branch.

    ``up_to_message_id`` is the user message whose history is copied; the new
    conversation starts from that point with ``new_content`` replacing it.
    """
    message_id: uuid.UUID
    new_content: str
