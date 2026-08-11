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


# ---- Task 10: typed multimodal message parts (additive) -------------------
# These let a request carry text/image/audio/file parts. They are validated
# against the model's ModelCapabilities by app.providers.multimodal.route_multimodal
# before dispatch. ``parts`` is optional and additive: existing requests with a
# plain string ``content`` keep working unchanged.
class TextPart(BaseModel):
    type: str = "text"
    text: str


class ImagePart(BaseModel):
    """An image input part (base64 data URL or opaque artifact reference)."""
    type: str = "image"
    data_url: str
    media_type: str = "image/png"


class AudioPart(BaseModel):
    """An audio input part (base64 data URL or opaque artifact reference)."""
    type: str = "audio"
    data_url: str
    media_type: str = "audio/wav"


class FilePart(BaseModel):
    """An opaque file reference (metadata only; never raw bytes inline)."""
    type: str = "file"
    filename: str
    media_type: str = "application/octet-stream"
    size: int = 0


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

    ``mode`` is the user-facing capability selector. The UI picker exposes two
    modes — ``speed`` (极速: single-agent, no multi-agent, fastest first token)
    and ``expert`` (专家: multi-agent research crew by default). Legacy values
    (auto | search | deep_research | create | data_analysis | debate) remain
    accepted for backward compatibility. The backend IntentRouter maps the mode
    to runtime/profile/tools. Legacy ``execution_mode``/``agent_profile`` still
    override the derived route when set explicitly.
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
    mode: str = "speed"               # speed | expert (UI picker); legacy: auto|search|deep_research|create|data_analysis|debate
    attachment_ids: list[uuid.UUID] = []
    # ---- Agent platform: legacy fields (still accepted) ----
    execution_mode: str = "auto"      # auto | chat | agent
    agent_profile: str = "general"    # general | research | analyst | ...
    # Explicit per-request opt-in to DEMO execution (canned, non-real answers).
    # Only honoured when AGENT_DEMO_MODE is also True (which is itself refused
    # in prod by the config guard). A normal chat turn NEVER sets this; it is
    # the single gate that lets the DemoStageExecutor stand in for a real model
    # so the multi-agent panel can be hand-verified without an LLM endpoint.
    # When True the runtime_selection/meta carries is_demo=True so the UI MUST
    # show a persistent "演示模式，内容非真实生成" warning.
    demo: bool = False


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
