from __future__ import annotations

import uuid
from datetime import datetime
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
    """A source backing an assistant answer.

    ``document_id`` is nullable because attachment/web sources have no KB
    Document row. Scores are debug/eval only — the UI shows qualitative
    relevance, never a raw confidence percentage.
    """
    document_id: uuid.UUID | None = None
    document_name: str = ""
    chunk_id: uuid.UUID | None = None
    chunk_index: int = 0
    snippet: str = ""
    score: float = 0.0
    # ---- Phase 1: provenance + multi-source types ----
    # web | document | attachment | database
    source_type: str = "document"
    url: str | None = None
    attachment_id: uuid.UUID | None = None
    page_number: int | None = None
    published_at: datetime | None = None
    accessed_at: datetime | None = None
    # Reranker score (debug/eval only; None when no reranker ran).
    rerank_score: float | None = None
    metadata: dict[str, Any] = {}


class ChatRequest(BaseModel):
    """POST /api/chat/stream body. conversation_id optional for ad-hoc chat.

    ``mode`` is the user-facing capability selector (auto | search |
    deep_research | create | data_analysis). The backend IntentRouter maps it
    to runtime/profile/tools. Legacy ``execution_mode``/``agent_profile`` are
    still accepted for backward compatibility and override the derived route
    when set explicitly.
    """
    conversation_id: uuid.UUID | None = None
    model_id: uuid.UUID | None = None
    knowledge_base_id: uuid.UUID | None = None
    # Phase 1+: search across multiple knowledge bases in one turn (multi-KB).
    knowledge_base_ids: list[uuid.UUID] = []
    content: str = ""
    regenerate: bool = False          # regenerate last assistant turn
    stream: bool = True
    enable_tools: bool = False        # explicit override (legacy / advanced)
    # ---- Phase 1: user-facing mode + attachments ----
    mode: str = "auto"                # auto | search | deep_research | create | data_analysis
    attachment_ids: list[uuid.UUID] = []
    # ---- Agent platform: legacy fields (still accepted) ----
    execution_mode: str = "auto"      # auto | chat | agent
    agent_profile: str = "general"    # general | research | analyst | ...


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


# ---- Phase 1+: research plan + run-control event payloads (reserved) -------
class ResearchPlanStep(BaseModel):
    id: str
    title: str
    description: str = ""
    sources: list[str] = []


class ResearchPlanEvent(BaseModel):
    """``research_plan`` / ``research_plan_updated`` SSE payload."""
    run_id: str
    status: str = "draft"             # draft | confirmed | rejected | updated
    summary: str = ""
    steps: list[ResearchPlanStep] = []
    requires_confirmation: bool = True


class RunInstructionEvent(BaseModel):
    """``run_instruction_received`` payload (user appended mid-run guidance)."""
    run_id: str
    instruction: str
    acknowledged: bool = True


class RunPauseEvent(BaseModel):
    run_id: str
    reason: str = "user"
    paused_at: datetime | None = None


class RunResumeEvent(BaseModel):
    run_id: str
    resumed_at: datetime | None = None
