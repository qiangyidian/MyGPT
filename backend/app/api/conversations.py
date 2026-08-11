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
from app.models import Conversation, KnowledgeBase, Message, ModelConfig, User
from app.schemas import (
    ConversationBranchRequest,
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    ConversationUpdate,
    MessageOut,
)
from app.services.conversation_service import branch_from_message, list_for_user

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

NOT_FOUND = status.HTTP_404_NOT_FOUND
# Cap on how many recent messages the detail endpoint returns in one response.
# selectinload would pull the ENTIRE history; an explicit capped query bounds
# memory + serialization for very long conversations. High enough that normal
# conversations are unaffected.
_DETAIL_MESSAGE_WINDOW = 1000


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


async def _validate_refs(
    db: AsyncSession,
    user: User,
    *,
    model_id: uuid.UUID | None = None,
    knowledge_base_id: uuid.UUID | None = None,
) -> None:
    """404 (not a 500 on FK violation) when a referenced model/kb doesn't exist
    or belongs to another user. Models may be system-wide (user_id NULL)."""
    if model_id is not None:
        mc = await db.get(ModelConfig, model_id)
        if mc is None or (mc.user_id is not None and mc.user_id != user.id):
            raise HTTPException(NOT_FOUND, "Model not found")
    if knowledge_base_id is not None:
        kb = await db.get(KnowledgeBase, knowledge_base_id)
        if kb is None or kb.user_id != user.id:
            raise HTTPException(NOT_FOUND, "Knowledge base not found")


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    q: str | None = None,
    archived: bool = False,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ConversationOut]:
    rows = await list_for_user(
        db, user.id, q=q, archived=archived, limit=limit, offset=offset
    )
    return [ConversationOut.model_validate(c) for c in rows]


@router.post("", response_model=ConversationDetail, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: ConversationCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConversationDetail:
    await _validate_refs(
        db, user, model_id=payload.model_id, knowledge_base_id=payload.knowledge_base_id
    )
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
    conv = await _load_owned(db, conv_id, user)
    # Load a bounded window of recent messages (oldest-first) rather than
    # selectinload-ing the entire history, which could serialize thousands of
    # rows for a long conversation.
    msg_rows = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(_DETAIL_MESSAGE_WINDOW)
        )
    ).scalars().all()
    detail = ConversationDetail.model_validate(conv)
    detail.messages = [MessageOut.model_validate(m) for m in reversed(msg_rows)]
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
    # Validate referenced ids before persisting (avoids a 500 on FK violation).
    await _validate_refs(
        db, user,
        model_id=data.get("model_id"),
        knowledge_base_id=data.get("knowledge_base_id"),
    )
    # Map the user-facing alias fields to the model columns.
    if "pinned" in data:
        conv.is_pinned = bool(data.pop("pinned"))
    if "archived" in data:
        conv.is_archived = bool(data.pop("archived"))
    if "title" in data and data["title"] is not None:
        data["title"] = (data["title"] or "").strip() or conv.title
    for field, value in data.items():
        setattr(conv, field, value)
    await db.commit()
    await db.refresh(conv)
    return ConversationOut.model_validate(conv)


@router.post("/{conv_id}/branch", response_model=ConversationDetail,
             status_code=status.HTTP_201_CREATED)
async def branch_conversation(
    conv_id: uuid.UUID,
    payload: ConversationBranchRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConversationDetail:
    """Edit-and-resend: fork history before a message into a new conversation."""
    await _load_owned(db, conv_id, user)
    branch = await branch_from_message(
        db,
        user_id=user.id,
        conversation_id=conv_id,
        message_id=payload.message_id,
        new_content=payload.new_content,
    )
    if branch is None:
        raise HTTPException(NOT_FOUND, "Conversation or message not found")
    detail = ConversationDetail.model_validate(branch)
    detail.messages = [
        MessageOut.model_validate(m)
        for m in sorted(branch.messages, key=lambda m: m.created_at)
    ]
    return detail


@router.get("/{conv_id}/branches")
async def list_branches(
    conv_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return the parent + child branches of a conversation (branch tree)."""
    conv = await _load_owned(db, conv_id, user)
    parent = None
    if conv.parent_conversation_id is not None:
        p = (
            await db.execute(
                select(Conversation).where(Conversation.id == conv.parent_conversation_id)
            )
        ).scalars().first()
        if p is not None and (p.user_id == user.id or user.role == "admin"):
            parent = ConversationOut.model_validate(p)
    children = (
        await db.execute(
            select(Conversation)
            .where(
                Conversation.parent_conversation_id == conv.id,
                Conversation.user_id == user.id,
            )
            .order_by(Conversation.updated_at.desc())
        )
    ).scalars().all()
    return {
        "current": ConversationOut.model_validate(conv),
        "parent": parent,
        "children": [ConversationOut.model_validate(c) for c in children],
    }


@router.delete("/{conv_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conv_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    conv = await _load_owned(db, conv_id, user)
    # Clean up attachment file bytes + per-attachment RAG vectors BEFORE the FK
    # CASCADE removes the rows — otherwise deleting a conversation leaks every
    # attachment's file on disk forever (cascade only deletes DB rows).
    from app.services import attachment_service

    await attachment_service.delete_for_conversation(db, conv.id)
    await db.delete(conv)
    await db.commit()
