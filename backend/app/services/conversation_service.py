"""Conversation CRUD + helpers.

All reads are scoped by ``user_id`` so one user can never see another user's
conversations. Titles are auto-derived from the first user message when the
conversation still carries the default placeholder title.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Conversation, Message
from app.schemas import ConversationCreate, ConversationUpdate

DEFAULT_TITLE = "新对话"
_AUTO_TITLE_MAX = 40


async def list_for_user(
    db: AsyncSession, user_id: uuid.UUID
) -> list[Conversation]:
    """Return all conversations owned by ``user_id``, newest first."""
    result = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.updated_at.desc())
    )
    return list(result.scalars().all())


async def get(
    db: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> Optional[Conversation]:
    """Fetch a conversation with messages, enforcing ownership.

    Returns None when the conversation does not exist or belongs to another user.
    """
    result = await db.execute(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def create(
    db: AsyncSession, user_id: uuid.UUID, data: ConversationCreate
) -> Conversation:
    """Create a new conversation for ``user_id``."""
    conv = Conversation(
        user_id=user_id,
        title=(data.title or "").strip() or DEFAULT_TITLE,
        model_id=data.model_id,
        knowledge_base_id=data.knowledge_base_id,
        system_prompt=data.system_prompt,
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


async def update(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    data: ConversationUpdate,
) -> Optional[Conversation]:
    """Patch-update a conversation (only supplied fields). Ownership-enforced."""
    conv = await get(db, conversation_id, user_id)
    if conv is None:
        return None
    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        if field == "title":
            value = (value or "").strip() or DEFAULT_TITLE
        setattr(conv, field, value)
    await db.commit()
    await db.refresh(conv)
    return conv


async def delete(
    db: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Delete a conversation. Returns True if something was deleted."""
    conv = await get(db, conversation_id, user_id)
    if conv is None:
        return False
    await db.delete(conv)
    await db.commit()
    return True


async def maybe_autotitle(
    db: AsyncSession,
    conversation: Conversation,
    first_message_content: str,
) -> Optional[str]:
    """If the conversation still has the default title, derive one from content.

    Returns the new title (and persists it) or None if no change was made.
    """
    if not first_message_content or not first_message_content.strip():
        return None
    if conversation.title and conversation.title != DEFAULT_TITLE:
        return None
    new_title = _truncate_title(first_message_content.strip())
    conversation.title = new_title
    await db.flush()
    return new_title


def _truncate_title(content: str) -> str:
    """Trim ``content`` to a sensible title length, respecting word boundaries."""
    text = content.replace("\n", " ").strip()
    if len(text) <= _AUTO_TITLE_MAX:
        return text
    cut = text[:_AUTO_TITLE_MAX]
    # Try to break on whitespace rather than mid-word.
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


async def first_message_autotitle(
    db: AsyncSession,
    conversation: Conversation,
    message: Message,
) -> Optional[str]:
    """Convenience wrapper: derive a title from a just-created user message."""
    if message.role != "user":
        return None
    return await maybe_autotitle(db, conversation, message.content)
