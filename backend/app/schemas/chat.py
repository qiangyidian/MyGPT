from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


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
    mode: str = "speed"               # speed | expert | hermes (UI picker); legacy: auto|search|deep_research|create|data_analysis|debate
    attachment_ids: list[uuid.UUID] = []
    # Reasoning-effort hint (B6). Honored only when the selected model config
    # declares supports_reasoning_effort; ignored otherwise (never an error).
    reasoning_effort: str | None = None   # low | medium | high
    # ---- Agent platform: legacy fields (still accepted) ----
    execution_mode: str = "auto"      # auto | chat | agent
    agent_profile: str = "general"    # general | research | analyst | ...
