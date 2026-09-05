"""Message-level actions: thumbs-up/down feedback (Phase 1).

One rating per (user, message). The message must belong to a conversation the
user owns (admins too can access). DELETE clears the user's rating.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db import get_db
from app.models import Conversation, Message, MessageFeedback, User
from app.schemas.feedback import MessageFeedbackOut, MessageFeedbackRequest

router = APIRouter(prefix="/api/messages", tags=["messages"])

NOT_FOUND = status.HTTP_404_NOT_FOUND


async def _load_owned_message(db: AsyncSession, message_id: uuid.UUID, user: User) -> Message:
    msg = await db.get(Message, message_id)
    if msg is None:
        raise HTTPException(NOT_FOUND, "Message not found")
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == msg.conversation_id))
    ).scalars().first()
    if conv is None or (conv.user_id != user.id and user.role != "admin"):
        # 404 (not 403) to avoid leaking existence.
        raise HTTPException(NOT_FOUND, "Message not found")
    return msg


@router.post("/{message_id}/feedback", response_model=MessageFeedbackOut)
async def set_feedback(
    message_id: uuid.UUID,
    payload: MessageFeedbackRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageFeedbackOut:
    msg = await _load_owned_message(db, message_id, user)
    # Upsert: one row per (user, message).
    existing = (
        await db.execute(
            select(MessageFeedback).where(
                MessageFeedback.message_id == msg.id,
                MessageFeedback.user_id == user.id,
            )
        )
    ).scalars().first()
    if existing is not None:
        existing.rating = payload.rating
        existing.reason = payload.reason
        existing.comment = payload.comment
        fb = existing
    else:
        fb = MessageFeedback(
            user_id=user.id,
            message_id=msg.id,
            conversation_id=msg.conversation_id,
            rating=payload.rating,
            reason=payload.reason,
            comment=payload.comment,
        )
        db.add(fb)
    await db.commit()
    await db.refresh(fb)
    return MessageFeedbackOut.model_validate(fb)


@router.delete("/{message_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feedback(
    message_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _load_owned_message(db, message_id, user)
    existing = (
        await db.execute(
            select(MessageFeedback).where(
                MessageFeedback.message_id == message_id,
                MessageFeedback.user_id == user.id,
            )
        )
    ).scalars().first()
    if existing is not None:
        await db.delete(existing)
        await db.commit()


@router.get("/{message_id}/feedback", response_model=MessageFeedbackOut | None)
async def get_feedback(
    message_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageFeedbackOut | None:
    await _load_owned_message(db, message_id, user)
    existing = (
        await db.execute(
            select(MessageFeedback).where(
                MessageFeedback.message_id == message_id,
                MessageFeedback.user_id == user.id,
            )
        )
    ).scalars().first()
    return MessageFeedbackOut.model_validate(existing) if existing else None
