"""chat_service 里可复用的纯 helper 的落位。

``app/agents/memory/*`` 需要 ``load_history`` / ``safe_int`` / ``messages_to_dicts`` /
``augment_with_attachments``，但不能 import ``app.services.chat_service``（会成环：
``chat_service`` 要 import ``facade``）。把这些函数搬到这里，``chat_service`` 再从本模块
import 回去 —— 函数体一字未改，只是换了个模块住。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Message
from app.security.prompt_boundary import apply_untrusted_boundary


def safe_int(value: Any, default: int) -> int:
    try:
        v = int(value)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


async def load_history(
    db: AsyncSession, conversation_id: uuid.UUID, *, limit: int = 500
) -> list[Message]:
    """Return the most recent ``limit`` conversation messages, oldest-first.

    Capped so a very long conversation doesn't load every message row (with full
    content/metadata) into memory on every turn — trimming + summarization then
    operate within this recent window. ``limit`` defaults high enough that normal
    conversations are unaffected; it's a backstop against pathological histories.
    """
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    rows.reverse()  # restore oldest-first for callers
    return rows


def augment_with_attachments(
    user_content: str, attachment_text: str, max_chars: int | None = 8000
) -> str:
    """Append parsed attachment text to the user content for this turn.

    The attachment bytes live in storage; only the extracted text is spliced in
    so a text-only model can reason over the file. Larger files / structured
    data are meant to go through file tools in data_analysis mode; here we keep
    a bounded inline snippet so it never blows the context budget.
    ``max_chars=None`` means the caller already bounded the text (the
    history-rehydration path caps per message) — do not truncate again.

    这里是附件文本进模型上下文的**最后一个**拼接点（当前轮与跨轮记忆两条路径都走
    它），所以围栏只加一次：文件正文属外部数据，包进不可信内容围栏后再拼上去。
    """
    snippet = (attachment_text or "").strip()
    if not snippet:
        return user_content
    # Bound the inline injection; full text stays on the attachment row.
    if max_chars is not None and len(snippet) > max_chars:
        snippet = snippet[:max_chars] + "\n…（内容已截断，完整内容见附件）"
    fenced = apply_untrusted_boundary("attachment", snippet)
    if not fenced:
        return user_content
    return f"{user_content}\n\n[附件内容]\n{fenced}"


def messages_to_dicts(
    system: str | None,
    history: list[Message],
    extra: list[dict[str, Any]] | None = None,
    attachment_text_by_id: dict[uuid.UUID, str] | None = None,
) -> list[dict[str, Any]]:
    """Flatten persisted Message rows + a system prompt into provider message dicts.

    ``extra`` carries already-shaped dicts (e.g. tool turns appended during the
    current turn) that are appended verbatim after the persisted history.
    ``attachment_text_by_id`` carries per-message bounded attachment context
    (keyed by Message.id): user messages with bound files get the extracted
    text appended so the model actually SEES the uploads — both the current
    turn and follow-up turns (cross-turn file memory). The persisted
    ``Message.content`` itself stays clean so the UI bubble never shows the
    spliced text.
    """
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})

    for msg in history:
        role = msg.role
        # Skip persisted system rows — we synthesized the system prompt above
        # (possibly with RAG context). Avoids a duplicate system turn.
        if role == "system":
            continue
        content = msg.content or ""
        if role == "user" and attachment_text_by_id and msg.id in attachment_text_by_id:
            content = augment_with_attachments(
                content, attachment_text_by_id[msg.id], max_chars=None
            )
        entry: dict[str, Any] = {"role": role, "content": content}
        meta = msg.metadata_ or {}
        # Preserve tool-call linkage so the model sees a coherent transcript.
        if role == "assistant" and meta.get("tool_calls"):
            entry["tool_calls"] = meta["tool_calls"]
        if role == "tool" and meta.get("tool_call_id"):
            entry["tool_call_id"] = meta["tool_call_id"]
            entry["name"] = meta.get("tool_name") or meta.get("name") or ""
        out.append(entry)

    if extra:
        out.extend(extra)
    return out
