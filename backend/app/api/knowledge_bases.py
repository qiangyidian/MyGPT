"""Knowledge-base router: list / create / get / delete.

A user only sees their own knowledge bases. Admins may access any. Counts
(documents / chunks) are aggregated so the UI can show them without extra calls.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db import get_db
from app.models import Document, DocumentChunk, KnowledgeBase, User
from app.schemas import KnowledgeBaseCreate, KnowledgeBaseOut

router = APIRouter(prefix="/api/knowledge-bases", tags=["knowledge-bases"])

NOT_FOUND = status.HTTP_404_NOT_FOUND


async def _load_owned(
    db: AsyncSession, kb_id: uuid.UUID, user: User, *, for_update: bool = False
) -> KnowledgeBase:
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(NOT_FOUND, "Knowledge base not found")
    if kb.user_id != user.id and user.role != "admin":
        raise HTTPException(NOT_FOUND, "Knowledge base not found")
    return kb


async def _counts(db: AsyncSession, kb_ids: list[uuid.UUID]) -> tuple[dict, dict]:
    """Return (doc_count_by_kb, chunk_count_by_kb) for the given KB ids.

    Scoped to the KBs being listed — the unscoped version aggregated the whole
    platform's documents/chunks (including other users') on every KB page load,
    which grows with global content volume, not the caller's.
    """
    if not kb_ids:
        return {}, {}
    doc_rows = await db.execute(
        select(Document.knowledge_base_id, func.count())
        .where(Document.knowledge_base_id.in_(kb_ids))
        .group_by(Document.knowledge_base_id)
    )
    doc_counts = {str(kb): n for kb, n in doc_rows.all()}
    chunk_rows = await db.execute(
        select(DocumentChunk.knowledge_base_id, func.count())
        .where(DocumentChunk.knowledge_base_id.in_(kb_ids))
        .group_by(DocumentChunk.knowledge_base_id)
    )
    chunk_counts = {str(kb): n for kb, n in chunk_rows.all()}
    return doc_counts, chunk_counts


def _to_out(kb: KnowledgeBase, doc_counts: dict, chunk_counts: dict) -> KnowledgeBaseOut:
    return KnowledgeBaseOut(
        id=kb.id,
        user_id=kb.user_id,
        name=kb.name,
        description=kb.description,
        embedding_model_id=kb.embedding_model_id,
        document_count=doc_counts.get(str(kb.id), 0),
        chunk_count=chunk_counts.get(str(kb.id), 0),
        created_at=kb.created_at,
    )


@router.get("", response_model=list[KnowledgeBaseOut])
async def list_knowledge_bases(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[KnowledgeBaseOut]:
    stmt = select(KnowledgeBase).where(KnowledgeBase.user_id == user.id).order_by(KnowledgeBase.created_at.desc())
    if user.role == "admin":
        stmt = select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc())
    res = await db.execute(stmt)
    kbs = list(res.scalars().all())
    doc_counts, chunk_counts = await _counts(db, [kb.id for kb in kbs])
    return [_to_out(kb, doc_counts, chunk_counts) for kb in kbs]


@router.post("", response_model=KnowledgeBaseOut, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeBaseOut:
    kb = KnowledgeBase(
        user_id=user.id,
        name=payload.name,
        description=payload.description,
        embedding_model_id=payload.embedding_model_id,
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return _to_out(kb, {}, {})


@router.get("/{kb_id}", response_model=KnowledgeBaseOut)
async def get_knowledge_base(
    kb_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeBaseOut:
    kb = await _load_owned(db, kb_id, user)
    doc_counts, chunk_counts = await _counts(db, [kb.id])
    return _to_out(kb, doc_counts, chunk_counts)


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    kb_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    kb = await _load_owned(db, kb_id, user)
    # Best-effort: drop the Qdrant collection's points for this KB.
    try:
        from app.rag.qdrant_store import get_vector_store
        from app.rag.rag_service import collection_name
        await get_vector_store().delete_by_filter(collection_name(kb.id), {})
    except Exception:  # noqa: BLE001
        pass
    await db.delete(kb)  # cascades documents + chunks
    await db.commit()
