"""Message-level actions: feedback rating, in-place content edit, truncate.

One rating per (user, message). The message must belong to a conversation the
user owns (admins too can access). DELETE clears the user's rating.

PATCH and DELETE /after both archive before they destroy: the row being
overwritten or removed is snapshotted into ``message_versions`` first, so
"fix a typo" and "clear everything after this turn" stop being silent data
loss (条目 30 / 31 / 33).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.exceptions import AppException
from app.db import get_db
from app.models import Conversation, Message, MessageFeedback, ToolCall, User
from app.schemas.feedback import MessageFeedbackOut, MessageFeedbackRequest
from app.schemas.message import (
    MessageContentUpdate,
    MessageOut,
    MessageTruncateResult,
)
from app.services.message_versions import snapshot_message

router = APIRouter(prefix="/api/messages", tags=["messages"])

NOT_FOUND = status.HTTP_404_NOT_FOUND


async def _load_owned_message(db: AsyncSession, message_id: uuid.UUID, user: User) -> Message:
    msg = await db.get(Message, message_id)
    if msg is None:
        raise HTTPException(NOT_FOUND, "消息不存在")
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == msg.conversation_id))
    ).scalars().first()
    if conv is None or (conv.user_id != user.id and user.role != "admin"):
        # 404 (not 403) to avoid leaking existence.
        raise HTTPException(NOT_FOUND, "消息不存在")
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


def _is_generating(msg: Message) -> bool:
    """这一轮还在流式生成中——此时改正文或截断都会和写回打架。"""
    meta = msg.metadata_ or {}
    return meta.get("status") == "pending" and not meta.get("finish_reason")


@router.patch("/{message_id}", response_model=MessageOut)
async def edit_message_content(
    message_id: uuid.UUID,
    payload: MessageContentUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    """改一条消息的正文；被改掉的原文先存成一版（条目 31）。

    只改正文，**不**顺带截断后面的消息：截断是不可逆的，得由界面显式发起
    （``DELETE /{id}/after``）。把两件事绑在一起，用户以为只是改了个错别字，
    结果后面十轮没了。
    """
    msg = await _load_owned_message(db, message_id, user)
    if _is_generating(msg):
        raise AppException(409, "turn_in_progress", "这条消息还在生成中，请先停止再修改")
    new_content = payload.content.strip()
    if not new_content:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "内容不能为空")
    if new_content == (msg.content or ""):
        return MessageOut.model_validate(msg)

    await snapshot_message(db, msg, origin="edit")
    msg.content = new_content
    # 用户改了提问，「这条已经回答过」的语义就不成立了：清掉赞踩之外，
    # 引用面板留着会指向没依据的原文，所以把引用一并作废。
    meta = dict(msg.metadata_ or {})
    meta.pop("citations", None)
    msg.metadata_ = meta
    await db.commit()
    await db.refresh(msg)
    return MessageOut.model_validate(msg)


@router.delete("/{message_id}/after", response_model=MessageTruncateResult)
async def truncate_after_message(
    message_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageTruncateResult:
    """删掉这条消息**之后**的所有消息；每条删除前先存一版。

    这是「改完提问重新问」的后半段：正文改好后把后面的轮次清掉，会话回到这条
    消息为止。删除是级联的（工具调用行跟着走），但版本行不跟着走——见
    ``app/models/message_version.py`` 里为什么不建外键。
    """
    anchor = await _load_owned_message(db, message_id, user)
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == anchor.conversation_id))
    ).scalars().first()
    later = list(
        (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == anchor.conversation_id)
                .order_by(Message.created_at.asc(), Message.id.asc())
            )
        )
        .scalars()
        .all()
    )
    doomed = [m for m in later if m.created_at > anchor.created_at and m.id != anchor.id]
    if any(_is_generating(m) for m in doomed):
        raise AppException(
            409, "turn_in_progress", "后面的回复还在生成中，请先停止再截断"
        )
    for msg in doomed:
        await snapshot_message(db, msg, origin="truncate")
        await db.execute(delete(ToolCall).where(ToolCall.message_id == msg.id))
        await db.delete(msg)

    preview = (anchor.content or "").strip()[:280]
    if conv is not None:
        conv.last_message_preview = preview
    await db.commit()
    return MessageTruncateResult(
        conversation_id=anchor.conversation_id,
        deleted=len(doomed),
        last_message_preview=preview,
    )
