"""Knowledge-base router: list / create / get / delete.

A user only sees their own knowledge bases. Admins may access any. Counts
(documents / chunks) are aggregated with GROUP BY queries scoped to the listed
KBs, so the UI can show them without extra calls and without loading rows.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db import get_db
from app.models import Document, DocumentChunk, KnowledgeBase, User
from app.schemas import KnowledgeBaseCreate, KnowledgeBaseOut
from app.services import document_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge-bases", tags=["knowledge-bases"])

NOT_FOUND = status.HTTP_404_NOT_FOUND

# Listing is paginated: the admin branch used to ``select(KnowledgeBase)`` over
# the whole platform with no cap. ``limit`` stays generous so existing clients
# that send no parameters keep seeing everything they plausibly have.
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


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
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[KnowledgeBaseOut]:
    """One page of knowledge bases, newest first.

    Users see their own; admins see every KB on the platform — which is exactly
    why the page cap is real: the unbounded version listed (and eager-loaded)
    the whole table on every request.
    """
    stmt = (
        select(KnowledgeBase)
        .order_by(KnowledgeBase.created_at.desc(), KnowledgeBase.id.desc())
    )
    if user.role != "admin":
        stmt = stmt.where(KnowledgeBase.user_id == user.id)
    res = await db.execute(stmt.limit(limit).offset(offset))
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
    # 整个 collection 必须删掉。原来这里调的是 delete_by_filter(collection, {})
    # —— 空名单在 _to_filter 里变成 None 并直接 return，也就是说**一次删除都没
    # 发生过**：行、文档、KB 全没了，向量还整份躺在 Qdrant 里，而且 collection
    # 名字由 kb id 派生，删掉 KB 后再也没有代码路径能指回它（孤儿泄漏）。
    #
    # 这里仍然不阻塞删除：Qdrant 不可达时把用户的「删掉我的知识库」变成 502 是
    # 更糟的失败。漏掉的 collection 由
    # :func:`app.services.retention.sweep_orphan_collections` 兜底回收。
    try:
        from app.rag.qdrant_store import get_vector_store
        from app.rag.rag_service import collection_name

        await get_vector_store().drop_collection(collection_name(kb.id))
    except Exception as exc:
        logger.warning("drop Qdrant collection for KB %s failed: %s", kb.id, exc)
    # Child rows go first, with explicit SQL: ``KnowledgeBase.documents`` is
    # lazy="raise", so ``db.delete(kb)`` can no longer load the collection to
    # cascade — and on SQLite the FK's ON DELETE CASCADE is not enforced either.
    await document_service.purge_kb_rows(db, [kb.id])
    await db.execute(sa_delete(KnowledgeBase).where(KnowledgeBase.id == kb.id))
    await db.commit()
