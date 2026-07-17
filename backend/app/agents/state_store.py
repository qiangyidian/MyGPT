"""Cross-turn conversation state: goals, rolling summaries, facts.

Backed by the ``conversation_memories`` table (one row per memory entry). The
state is reconstructed on each turn from the latest summary + accumulated facts
+ the most recent user goal, so a multi-turn agent remembers what it's working
on even after the raw message history is trimmed.

All reads/writes are scoped by ``conversation_id`` (+ ``user_id``) so memories
never cross tenants.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.schemas import ConversationFlowState
from app.models import ConversationMemory


async def load_state(
    db: AsyncSession, conversation_id: uuid.UUID, user_id: Optional[uuid.UUID]
) -> ConversationFlowState:
    """Rebuild the flow state from persisted memories."""
    mems = (
        await db.execute(
            select(ConversationMemory)
            .where(ConversationMemory.conversation_id == conversation_id)
            .order_by(ConversationMemory.created_at.asc())
        )
    ).scalars().all()

    summary = ""
    goal = ""
    facts: list[dict] = []
    for m in mems:
        if m.memory_type == "summary":
            summary = m.content  # latest summary wins
        elif m.memory_type == "task":
            goal = m.content  # latest task goal wins
        elif m.memory_type == "fact":
            facts.append(
                {"content": m.content, "confirmed": m.confirmed_by_user}
            )

    return ConversationFlowState(
        conversation_id=str(conversation_id),
        user_id=str(user_id) if user_id else "",
        user_goal=goal,
        conversation_summary=summary,
        long_term_facts=facts,
    )


async def upsert_goal(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    user_id: Optional[uuid.UUID],
    goal: str,
    source_message_id: Optional[uuid.UUID] = None,
) -> None:
    """Set the conversation's current goal (single 'task' memory, updated)."""
    if not goal.strip():
        return
    existing = (
        await db.execute(
            select(ConversationMemory).where(
                ConversationMemory.conversation_id == conversation_id,
                ConversationMemory.memory_type == "task",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.content = goal.strip()
        if source_message_id:
            existing.source_message_id = source_message_id
    else:
        db.add(
            ConversationMemory(
                conversation_id=conversation_id,
                user_id=user_id,
                memory_type="task",
                content=goal.strip(),
                source_message_id=source_message_id,
                confidence=1.0,
            )
        )
    await db.flush()


async def save_summary(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    user_id: Optional[uuid.UUID],
    summary: str,
    source_message_id: Optional[uuid.UUID] = None,
) -> None:
    """Append a rolling summary memory (the latest one wins on load)."""
    if not summary.strip():
        return
    db.add(
        ConversationMemory(
            conversation_id=conversation_id,
            user_id=user_id,
            memory_type="summary",
            content=summary.strip(),
            source_message_id=source_message_id,
            confidence=0.9,
        )
    )
    await db.flush()


async def save_fact(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    user_id: Optional[uuid.UUID],
    fact: str,
    *,
    confirmed: bool = False,
    source_message_id: Optional[uuid.UUID] = None,
) -> None:
    if not fact.strip():
        return
    db.add(
        ConversationMemory(
            conversation_id=conversation_id,
            user_id=user_id,
            memory_type="fact",
            content=fact.strip(),
            confirmed_by_user=confirmed,
            source_message_id=source_message_id,
            confidence=1.0 if confirmed else 0.5,
        )
    )
    await db.flush()
