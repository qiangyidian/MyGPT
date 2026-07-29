"""Conversation memories router (Phase 3 — user memory management).

Exposes the long-lived memories (facts/preferences/summaries/tasks) the agent
writes during a conversation so the user can inspect, confirm, edit, and delete
them. Always scoped by user_id + conversation_id.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db import get_db
from app.models import Conversation, ConversationMemory, User
from app.schemas import MemoryOut, MemoryUpdate

router = APIRouter(prefix="/api/conversations", tags=["memories"])

NOT_FOUND = status.HTTP_404_NOT_FOUND


async def _assert_owned(db: AsyncSession, conversation_id: uuid.UUID, user: User) -> None:
    conv = await db.get(Conversation, conversation_id)
    if conv is None or (conv.user_id != user.id and user.role != "admin"):
        raise HTTPException(NOT_FOUND, "Conversation not found")


@router.get("/{conversation_id}/memories", response_model=list[MemoryOut])
async def list_memories(
    conversation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MemoryOut]:
    await _assert_owned(db, conversation_id, user)
    res = await db.execute(
        select(ConversationMemory)
        .where(
            ConversationMemory.conversation_id == conversation_id,
            ConversationMemory.user_id == user.id,
        )
        .order_by(ConversationMemory.created_at.desc())
    )
    return [MemoryOut.model_validate(m) for m in res.scalars().all()]


@router.patch("/memories/{memory_id}", response_model=MemoryOut)
async def update_memory(
    memory_id: uuid.UUID,
    payload: MemoryUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MemoryOut:
    m = await db.get(ConversationMemory, memory_id)
    if m is None or m.user_id != user.id:
        raise HTTPException(NOT_FOUND, "Memory not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(m, field, value)
    await db.commit()
    await db.refresh(m)
    return MemoryOut.model_validate(m)


@router.delete("/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    memory_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    m = await db.get(ConversationMemory, memory_id)
    if m is None or m.user_id != user.id:
        raise HTTPException(NOT_FOUND, "Memory not found")
    await db.delete(m)
    await db.commit()
