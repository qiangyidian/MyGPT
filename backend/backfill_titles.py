"""Backfill LLM titles for conversations created before auto-titling shipped.

maybe_autotitle only runs on new turns, so older conversations either stay on
the default "新对话" or hold the cheap truncated fallback. This script walks
them and runs the same chat-time pipeline:

  * default title          → maybe_autotitle (truncated fallback → LLM title)
  * truncated fallback     → maybe_autotitle_after_answer (LLM refine pass)
  * anything else          → untouched (treated as a user rename)

Run from the backend/ directory so backend/.env is loaded:

    cd backend && python backfill_titles.py

Idempotent: refined conversations are skipped on re-run.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models import Conversation, Message, ModelConfig
from app.services.title_service import (
    DEFAULT_TITLE,
    _generate_llm_title,
    is_default_title,
    maybe_autotitle,
    maybe_autotitle_after_answer,
    truncate_title,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill-titles")


async def _resolve_cfg(db, conversation: Conversation) -> ModelConfig | None:
    """Mirror chat_service._resolve_model_config's priority, minus the raise."""
    if conversation.model_id is not None:
        cfg = await db.get(ModelConfig, conversation.model_id)
        if cfg is not None:
            return cfg
    result = await db.execute(
        select(ModelConfig)
        .where(ModelConfig.is_embedding.is_(False))
        .order_by(ModelConfig.created_at.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _first_turn(db, conversation_id) -> tuple[str, str]:
    """First user message and first assistant reply prefix of the conversation."""
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.role.in_(["user", "assistant"]))
        .order_by(Message.created_at.asc())
        .limit(2)
    )
    first_user = ""
    first_assistant = ""
    for msg in result.scalars():
        if msg.role == "user" and not first_user:
            first_user = msg.content or ""
        elif msg.role == "assistant" and not first_assistant:
            first_assistant = (msg.content or "")[:400]
    return first_user, first_assistant


async def main() -> None:
    async with AsyncSessionLocal() as db:
        # Fetch every conversation — cheap at this scale — and filter in
        # Python, because "is this title the truncated fallback?" needs
        # truncate_title() of the first message, which SQL can't express.
        result = await db.execute(
            select(Conversation).order_by(Conversation.created_at.desc())
        )
        conversations = result.scalars().all()
        logger.info("scanning %d conversation(s)", len(conversations))

        titled = failed = 0
        for conv in conversations:
            first_user, first_assistant = await _first_turn(db, conv.id)
            if not first_user.strip():
                logger.info("skip %s (no user message)", conv.id)
                continue
            cfg = await _resolve_cfg(db, conv)
            if cfg is None:
                logger.warning("skip %s (no model config available)", conv.id)
                continue
            try:
                if is_default_title(conv.title):
                    changed = await maybe_autotitle(
                        db, conv, cfg,
                        first_user_message=first_user,
                        assistant_prefix=first_assistant,
                    )
                elif conv.title == truncate_title(first_user):
                    # Truncated-fallback title — the LLM refine pass upgrades
                    # it and still hands off user renames.
                    changed = await maybe_autotitle_after_answer(
                        db, conv, cfg,
                        first_user_message=first_user,
                        assistant_prefix=first_assistant,
                    )
                    # An error turn can leave the first assistant reply empty,
                    # which makes the refine pass a no-op — fall back to a
                    # direct LLM title from the user message alone.
                    if not changed and not first_assistant.strip():
                        llm_title = await _generate_llm_title(cfg, first_user, "")
                        if llm_title:
                            conv.title = llm_title
                            try:
                                await db.commit()
                                changed = True
                            except Exception:  # noqa: BLE001
                                await db.rollback()
                else:
                    continue  # user-renamed — hands off
            except Exception:  # noqa: BLE001 — one bad conversation must not stop the run
                logger.warning("title failed for %s", conv.id, exc_info=True)
                failed += 1
                continue
            if changed:
                titled += 1
                logger.info("titled %s: %s", conv.id, conv.title)
            else:
                failed += 1

        logger.info("done: %d titled, %d skipped/failed", titled, failed)


if __name__ == "__main__":
    asyncio.run(main())
