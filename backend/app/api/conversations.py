"""Conversations router: list / create / get (with messages) / update / delete.

A user only sees their own conversations. 404 (not 403) on foreign ids so ownership
is not leaked. Admins may access any conversation.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.deps import get_current_user
from app.db import get_db
from app.models import Conversation, Message, User
from app.schemas import (
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    ConversationUpdate,
    MessageOut,
)

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

NOT_FOUND = status.HTTP_404_NOT_FOUND


async def _load_owned(
    db: AsyncSession, conv_id: uuid.UUID, user: User, *, with_messages: bool = False
) -> Conversation:
    opts = [selectinload(Conversation.messages)] if with_messages else []
    stmt = select(Conversation).where(Conversation.id == conv_id)
    if opts:
        stmt = stmt.options(*opts)
    conv = (await db.execute(stmt)).scalars().first()
    if conv is None:
        raise HTTPException(NOT_FOUND, "Conversation not found")
    if conv.user_id != user.id and user.role != "admin":
        raise HTTPException(NOT_FOUND, "Conversation not found")
    return conv


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ConversationOut]:
    stmt = (
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc())
    )
    res = await db.execute(stmt)
    return [ConversationOut.model_validate(c) for c in res.scalars().all()]


@router.post("", response_model=ConversationDetail, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: ConversationCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConversationDetail:
    conv = Conversation(
        user_id=user.id,
        title=payload.title or "新对话",
        model_id=payload.model_id,
        knowledge_base_id=payload.knowledge_base_id,
        system_prompt=payload.system_prompt,
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    detail = ConversationDetail.model_validate(conv)
    detail.messages = []  # brand-new conversation has no messages yet
    return detail


@router.get("/{conv_id}", response_model=ConversationDetail)
async def get_conversation(
    conv_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConversationDetail:
    conv = await _load_owned(db, conv_id, user, with_messages=True)
    detail = ConversationDetail.model_validate(conv)
    detail.messages = [MessageOut.model_validate(m) for m in sorted(conv.messages, key=lambda m: m.created_at)]
    return detail


@router.patch("/{conv_id}", response_model=ConversationOut)
async def update_conversation(
    conv_id: uuid.UUID,
    payload: ConversationUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConversationOut:
    conv = await _load_owned(db, conv_id, user)
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(conv, field, value)
    await db.commit()
    await db.refresh(conv)
    return ConversationOut.model_validate(conv)


@router.delete("/{conv_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conv_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    conv = await _load_owned(db, conv_id, user)
    await db.delete(conv)
    await db.commit()
