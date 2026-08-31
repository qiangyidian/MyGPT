"""Conversation auto-titling — ChatGPT-style sidebar titles.

A conversation starts life as "新对话" (the frontend creates it with no title
before the first chat turn). After the FIRST user message this service gives
it a real title:

  1. immediately — a cheap truncated copy of the user message (≤24 chars, so
     the sidebar shows something meaningful within milliseconds), and
  2. best-effort — an LLM-generated concise title that overwrites the
     truncated one when it succeeds (a short non-streaming chat call through
     the SAME model config the conversation already uses; no extra model
     setup required).

Guardrails:
  * A conversation the user renamed themselves is never touched (we only
    touch conversations whose title is still the default sentinel).
  * Only the FIRST qualifying turn titles a conversation — once a real title
    exists (LLM or truncated), later turns don't rename it again.
  * Every failure path is silent (log + keep whatever title exists) —
    titling must never fail a chat turn.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.model_config import ModelConfig

logger = logging.getLogger(__name__)

# The default title the conversation is born with (model default + both
# creation paths). Exactly this value means "never titled yet".
DEFAULT_TITLE = "新对话"

# Truncation budgets for the immediate fallback title.
_FALLBACK_MAX_CHARS = 24
_LLM_MAX_CHARS = 30


def is_default_title(title: str | None) -> bool:
    """True when the title is still the placeholder (untitled)."""
    return not (title or "").strip() or (title or "").strip() == DEFAULT_TITLE


def truncate_title(text: str, max_chars: int = _FALLBACK_MAX_CHARS) -> str:
    """Collapse whitespace and truncate to ``max_chars`` (no ellipsis in the
    stored title — the UI truncates visually)."""
    collapsed = " ".join((text or "").split())
    return collapsed[:max_chars]


_TITLE_PROMPT = (
    "根据下面的对话开头，生成一个 4-12 个字的中文对话标题。要求：概括用户的核心意图，"
    "陈述式（不加书名号/引号/句号），不要出现\"对话\"\"提问\"等词。\n"
    "只输出标题本身，不要任何其他内容。\n\n"
    "用户消息：{first_message}\n\n助手回复开头：{assistant_prefix}"
)


def build_title_prompt(first_user_message: str, assistant_prefix: str) -> str:
    return _TITLE_PROMPT.format(
        first_message=(first_user_message or "").strip()[:400],
        assistant_prefix=(assistant_prefix or "").strip()[:400],
    )


def clean_llm_title(raw: str) -> str | None:
    """Sanitize the model's title output; None when unusable."""
    text = (raw or "").strip()
    # Strip common wrapper artifacts (quotes, markdown, "标题：" prefix).
    for wrapper in ('"', "'", "“", "”", "「", "」", "《", "》", "**", "#"):
        text = text.replace(wrapper, "")
    text = text.strip()
    if text.startswith("标题:"):
        text = text[3:].strip()
    if text.startswith("标题："):
        text = text[3:].strip()
    # Reject junk: too short, too long, or just the default.
    if not text or len(text) < 2:
        return None
    if len(text) > _LLM_MAX_CHARS:
        # Over-budget output is still often good — keep the front.
        text = text[:_LLM_MAX_CHARS]
    if text == DEFAULT_TITLE:
        return None
    return truncate_title(text, _LLM_MAX_CHARS)


async def _generate_llm_title(
    cfg: ModelConfig, first_user_message: str, assistant_prefix: str
) -> str | None:
    """One cheap non-streaming call → concise title, or None on any failure."""
    from app.providers.base import ChatOptions
    from app.providers.registry import get_provider_for_config

    try:
        provider = get_provider_for_config(cfg)
        result = await provider.chat(
            [{"role": "user", "content": build_title_prompt(first_user_message, assistant_prefix)}],
            # Budget must cover reasoning models' thinking blocks — at 48 the
            # GLM thinking phase consumed everything and text came back empty.
            ChatOptions(temperature=0.3, max_tokens=512),
        )
        return clean_llm_title(result.content or "")
    except Exception:  # noqa: BLE001 — titling is best-effort, never fatal
        logger.debug("LLM title generation failed", exc_info=True)
        return None


async def maybe_autotitle(
    db: AsyncSession,
    conversation: Conversation,
    cfg: ModelConfig | None,
    *,
    first_user_message: str,
    assistant_prefix: str = "",
    commit: bool = True,
) -> bool:
    """Title an untitled conversation; True when the title changed.

    Called right after the first user message is persisted (inline path) and
    where the durable path records its preview. Sets the truncated fallback
    immediately (so the sidebar has content at once); then, when the turn has
    an assistant prefix and a model config, tries the LLM title and overwrites
    the fallback in the same transaction.

    ``assistant_prefix`` empty (mid-turn call) → only the cheap fallback is
    set now; the LLM pass can be run later by calling this again with the
    prefix once the answer streams — by then the title is no longer default,
    so pass ``force_llm=True`` from that call site.
    """
    if conversation is None:
        return False
    current = (conversation.title or "").strip()
    if current and current != DEFAULT_TITLE:
        return False  # already titled (possibly by the user) — hands off

    text = (first_user_message or "").strip()
    if not text:
        return False

    changed = False
    fallback = truncate_title(text)
    if fallback and fallback != current:
        conversation.title = fallback
        changed = True

    if cfg is not None and (assistant_prefix or "").strip():
        llm_title = await _generate_llm_title(cfg, text, assistant_prefix)
        if llm_title:
            conversation.title = llm_title
            changed = True

    if changed and commit:
        try:
            await db.commit()
        except Exception:  # noqa: BLE001 — never fail the turn on title commit
            logger.warning("auto-title commit failed", exc_info=True)
            await db.rollback()
    return changed


async def maybe_autotitle_after_answer(
    db: AsyncSession,
    conversation: Conversation,
    cfg: ModelConfig | None,
    *,
    first_user_message: str,
    assistant_prefix: str,
) -> bool:
    """Second-stage titling after the answer streamed: LLM refinement pass.

    Replaces a truncated fallback title with an LLM one. Skips conversations
    that were titled by the user in the meantime — detected by the title no
    longer being the fallback we wrote (any non-default, non-fallback value
    is treated as user-owned).
    """
    if conversation is None:
        return False
    current = (conversation.title or "").strip()
    if not current or current == DEFAULT_TITLE:
        return False
    # If the current title is NOT the truncation of the first message, it's
    # either an LLM title or a user rename — leave it alone.
    if current != truncate_title(first_user_message):
        return False
    if cfg is None or not (assistant_prefix or "").strip():
        return False

    llm_title = await _generate_llm_title(cfg, first_user_message, assistant_prefix)
    if not llm_title:
        return False
    conversation.title = llm_title
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("auto-title refine commit failed", exc_info=True)
        await db.rollback()
        return False
    return True


def title_metadata(title: str) -> dict[str, Any]:
    """Optional provenance marker (unused for now; kept for future analytics)."""
    return {"auto_titled": title != DEFAULT_TITLE}
