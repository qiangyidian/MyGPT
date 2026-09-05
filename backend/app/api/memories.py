"""Conversation memories router (Phase 3 — user memory management).

Exposes the long-lived memories (facts/preferences/summaries/tasks) the agent
writes during a conversation so the user can inspect, confirm, edit, and delete
them. Always scoped by user_id + conversation_id.

Task 7 adds USER-level semantic memory controls under ``/api/memories`` — the
opt-in cross-conversation memory (activate / deactivate / edit / delete /
disable) layered on top of the existing conversation-scoped CRUD. The existing
``/api/conversations/{id}/memories`` routes are unchanged.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.memory_service import MemoryService
from app.core.deps import get_current_user
from app.db import get_db
from app.models import Conversation, ConversationMemory, ModelConfig, User
from app.providers.registry import get_provider_for_config
from app.rag.embedder import ProviderEmbedder
from app.rag.qdrant_store import get_vector_store
from app.schemas import (
    MemoryOut,
    MemoryUpdate,
    UserMemoryBulkAction,
    UserMemoryEdit,
    UserMemoryOut,
    UserMemoryPropose,
)

router = APIRouter(prefix="/api/conversations", tags=["memories"])
user_router = APIRouter(prefix="/api/memories", tags=["user-memories"])

NOT_FOUND = status.HTTP_404_NOT_FOUND


async def _resolve_embedder(db: AsyncSession) -> ProviderEmbedder | None:
    """Build an embedder from the first available embedding ModelConfig, or None.

    Mirrors :func:`app.rag.attachment_rag._resolve_embedder` so memory embedding
    uses the SAME provider layer as RAG (no parallel embedding code). Returns
    None when no embedding config exists — callers degrade gracefully (no
    retrieval until an embedder is configured).
    """
    res = await db.execute(
        select(ModelConfig)
        .where(ModelConfig.is_embedding.is_(True))
        .order_by(ModelConfig.created_at.asc())
        .limit(1)
    )
    cfg = res.scalar_one_or_none()
    if cfg is None:
        return None
    return ProviderEmbedder(get_provider_for_config(cfg), model=cfg.embedding_model_name)


def _embed_fn_from(embedder: ProviderEmbedder | None):
    """Adapt a ProviderEmbedder to the MemoryService embed_fn signature."""

    async def _embed(texts):
        if embedder is None:
            # No embedding config: return zero-dim vectors so the service stays
            # callable (retrieval will return no hits).
            return [[] for _ in texts]
        return await embedder.embed(texts)

    return _embed


async def get_memory_service(db: AsyncSession) -> MemoryService:
    """Build a MemoryService wired to the real embedder + Qdrant store.

    The MemoryService itself is pure-core (injected embed_fn + vector_store);
    this factory is the thin production adapter over the existing RAG wiring.
    """
    embedder = await _resolve_embedder(db)
    return MemoryService(
        embed_fn=_embed_fn_from(embedder),
        vector_store=get_vector_store(),
    )


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


# --------------------------------------------------------------------------- #
# Task 7: USER-level semantic memory (opt-in, cross-conversation)
# --------------------------------------------------------------------------- #
async def _assert_user_memory_owned(db: AsyncSession, memory_id: uuid.UUID, user: User):
    from app.models import UserMemory

    m = await db.get(UserMemory, memory_id)
    if m is None or m.user_id != user.id:
        raise HTTPException(NOT_FOUND, "Memory not found")
    return m


@user_router.get("", response_model=list[UserMemoryOut])
async def list_user_memories(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[UserMemoryOut]:
    from app.models import UserMemory

    res = await db.execute(
        select(UserMemory)
        .where(UserMemory.user_id == user.id)
        .order_by(UserMemory.updated_at.desc())
    )
    return [UserMemoryOut.model_validate(m) for m in res.scalars().all()]


@user_router.post("", response_model=UserMemoryOut, status_code=status.HTTP_201_CREATED)
async def propose_user_memory(
    payload: UserMemoryPropose,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserMemoryOut:
    """Propose a candidate user memory. Created INACTIVE — the user must
    activate it (opt-in) before it's embedded or retrieved."""
    service = await get_memory_service(db)
    memory = await service.propose(
        db,
        user.id,
        payload.content,
        memory_type=payload.memory_type,
        confidence=payload.confidence,
        source_message_id=payload.source_message_id,
        source_conversation_id=payload.source_conversation_id,
    )
    await db.commit()
    await db.refresh(memory)
    return UserMemoryOut.model_validate(memory)


@user_router.post("/bulk", response_model=dict)
async def bulk_set_user_memories(
    payload: UserMemoryBulkAction,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Bulk activate/deactivate all of the user's memories. Setting
    ``active=false`` is the "disable memory feature" path (removes every
    memory from the effective prompt without deleting rows)."""
    if payload.active:
        from app.models import UserMemory

        res = await db.execute(
            select(UserMemory).where(
                UserMemory.user_id == user.id,
                UserMemory.active.is_(False),
            )
        )
        service = await get_memory_service(db)
        count = 0
        for m in res.scalars().all():
            await service.activate(db, m.id)
            count += 1
        await db.commit()
        return {"activated": count}
    else:
        service = await get_memory_service(db)
        count = await service.disable(db, user.id)
        await db.commit()
        return {"deactivated": count}


@user_router.post("/{memory_id}/activate", response_model=UserMemoryOut)
async def activate_user_memory(
    memory_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserMemoryOut:
    """Activate a candidate: embed it and make it retrievable into the prompt."""
    await _assert_user_memory_owned(db, memory_id, user)
    service = await get_memory_service(db)
    memory = await service.activate(db, memory_id)
    await db.commit()
    await db.refresh(memory)
    return UserMemoryOut.model_validate(memory)


@user_router.post("/{memory_id}/deactivate", response_model=UserMemoryOut)
async def deactivate_user_memory(
    memory_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserMemoryOut:
    """Deactivate a memory: remove it from the prompt without deleting the row."""
    await _assert_user_memory_owned(db, memory_id, user)
    service = await get_memory_service(db)
    memory = await service.deactivate(db, memory_id)
    await db.commit()
    await db.refresh(memory)
    return UserMemoryOut.model_validate(memory)


@user_router.patch("/{memory_id}", response_model=UserMemoryOut)
async def edit_user_memory(
    memory_id: uuid.UUID,
    payload: UserMemoryEdit,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserMemoryOut:
    """Edit a memory's content (re-embeds if active so retrieval reflects it)."""
    await _assert_user_memory_owned(db, memory_id, user)
    service = await get_memory_service(db)
    memory = await service.edit(db, memory_id, payload.content)
    await db.commit()
    await db.refresh(memory)
    return UserMemoryOut.model_validate(memory)


@user_router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_memory(
    memory_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _assert_user_memory_owned(db, memory_id, user)
    service = await get_memory_service(db)
    await service.delete(db, memory_id)
    await db.commit()
