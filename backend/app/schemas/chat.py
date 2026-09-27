from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator

# How many knowledge bases one turn may read from. Retrieval fans out per KB
# (its own collection, possibly its own embedding model), so an uncapped list is
# a per-turn cost multiplier the caller controls — and no merged context can hold
# more than one prompt's worth of chunks anyway.
MAX_KB_PER_REQUEST = 5
# ``@`` mentions of any kind (KB / document / attachment) in one message.
MAX_MENTIONS_PER_REQUEST = 8
MENTION_KINDS = ("kb", "doc", "file")


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


class ChatMention(BaseModel):
    """One ``@`` reference resolved by the composer's inline picker.

    The client sends the pair it decoded out of the message text — never a label
    — so a renamed document cannot change what a turn retrieves. ``kind`` maps to
    the retrieval surface: ``kb`` adds a knowledge base to the turn's scope,
    ``doc`` narrows retrieval to that document, ``file`` binds an existing chat
    attachment to the outgoing message.
    """

    kind: str
    id: uuid.UUID

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, v: str) -> str:
        if v not in MENTION_KINDS:
            raise ValueError(f"kind 必须是 {'/'.join(MENTION_KINDS)} 之一")
        return v


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
    # ``@``-references typed in the composer (KB / document / attachment). They
    # EXTEND the toolbar's multi-select for this turn; ownership is checked where
    # the KB set is resolved (chat_service), not here.
    mentions: list[ChatMention] = []
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

    @field_validator("knowledge_base_ids")
    @classmethod
    def _cap_knowledge_bases(cls, v: list[uuid.UUID]) -> list[uuid.UUID]:
        """Collapse repeats, keep order, and refuse an over-fan-out request.

        A repeated id is a client bug rather than an attack (the same KB's
        collection would be fetched twice), so it is quietly dropped. The cap is
        a 422: it fires before the SSE stream opens, so the caller still gets a
        real status code instead of an error frame mid-stream.
        """
        out = list(dict.fromkeys(v))
        if len(out) > MAX_KB_PER_REQUEST:
            raise ValueError(f"一次最多选择 {MAX_KB_PER_REQUEST} 个知识库")
        return out

    @field_validator("mentions")
    @classmethod
    def _cap_mentions(cls, v: list[ChatMention]) -> list[ChatMention]:
        out: list[ChatMention] = []
        seen: set[tuple[str, uuid.UUID]] = set()
        for mention in v:
            key = (mention.kind, mention.id)
            if key in seen:
                continue
            seen.add(key)
            out.append(mention)
        if len(out) > MAX_MENTIONS_PER_REQUEST:
            raise ValueError(f"一次最多引用 {MAX_MENTIONS_PER_REQUEST} 个目标")
        return out
