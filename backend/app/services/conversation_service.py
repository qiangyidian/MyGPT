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
from sqlalchemy.orm import noload, selectinload

from app.models import Conversation, Message
from app.schemas import ConversationCreate, ConversationUpdate

DEFAULT_TITLE = "新对话"
_AUTO_TITLE_MAX = 40


async def list_for_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    q: str | None = None,
    archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[Conversation]:
    """Return the user's conversations, pinned-first then newest, with search.

    ``archived`` selects the archived set (default: only active conversations).
    ``q`` is a case-insensitive title substring. Limit/offset paginate.
    """
    stmt = select(Conversation).where(
        Conversation.user_id == user_id,
        Conversation.is_archived.is_(archived),
    ).options(noload(Conversation.messages))  # sidebar list must not pull messages
    if q:
        stmt = stmt.where(Conversation.title.ilike(f"%{q}%"))
    stmt = stmt.order_by(
        Conversation.is_pinned.desc(),
        Conversation.updated_at.desc(),
    ).limit(max(1, min(limit, 200))).offset(max(0, offset))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get(
    db: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> Optional[Conversation]:
    """Fetch a conversation, enforcing ownership.

    Returns None when the conversation does not exist or belongs to another user.
    Messages are NOT eager-loaded (the model relationship is default-lazy);
    callers that need history query Message explicitly.
    """
    result = await db.execute(
        select(Conversation).where(
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


async def branch_from_message(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    new_content: str | None = None,
) -> Conversation:
    """Create a branch: copy history *before* ``message_id`` into a new
    conversation, then return it. The caller sends ``new_content`` as a fresh
    chat turn to the returned conversation.

    The source conversation is never mutated. ``parent_conversation_id`` and
    ``branch_from_message_id`` link the branch back for traceability.
    """
    src = await get(db, conversation_id, user_id)
    if src is None:
        return None  # caller maps to 404

    target = await db.get(Message, message_id)
    if target is None or target.conversation_id != conversation_id:
        return None

    # History strictly before the edited message (oldest-first).
    res = await db.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.created_at < target.created_at,
        )
        .order_by(Message.created_at.asc())
    )
    prior = list(res.scalars().all())

    title_hint = (new_content or target.content or src.title or "").strip()
    branch = Conversation(
        user_id=user_id,
        title=_truncate_title(title_hint) or src.title,
        model_id=src.model_id,
        knowledge_base_id=src.knowledge_base_id,
        system_prompt=src.system_prompt,
        parent_conversation_id=src.id,
        branch_from_message_id=message_id,
    )
    db.add(branch)
    await db.flush()  # populate branch.id

    for m in prior:
        if m.role == "system":
            continue
        db.add(Message(
            conversation_id=branch.id,
            role=m.role,
            content=m.content or "",
            model_name=m.model_name,
            metadata_=dict(m.metadata_ or {}),
        ))
    await db.commit()
    await db.refresh(branch)
    return branch


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
