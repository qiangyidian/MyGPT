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
from app.models import Document, DocumentChunk, KnowledgeBase, ModelConfig, User
from app.schemas import KnowledgeBaseCreate, KnowledgeBaseOut, KnowledgeBaseUpdate
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
        top_k=kb.top_k,
        score_threshold=kb.score_threshold,
        rerank_enabled=kb.rerank_enabled,
        chunk_size=kb.chunk_size,
        chunk_overlap=kb.chunk_overlap,
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
    if payload.embedding_model_id is not None:
        await _validate_embedding_model(db, payload.embedding_model_id)
    kb = KnowledgeBase(
        user_id=user.id,
        name=payload.name,
        description=payload.description,
        embedding_model_id=payload.embedding_model_id,
        top_k=payload.top_k,
        score_threshold=payload.score_threshold,
        rerank_enabled=payload.rerank_enabled,
        chunk_size=payload.chunk_size,
        chunk_overlap=payload.chunk_overlap,
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return _to_out(kb, {}, {})


# Fields the PATCH may write. ``None`` on any of them means "inherit the
# platform default", so clearing an override is a real action, not a no-op.
_PATCHABLE = (
    "name",
    "description",
    "embedding_model_id",
    "top_k",
    "score_threshold",
    "rerank_enabled",
    "chunk_size",
    "chunk_overlap",
)


@router.patch("/{kb_id}", response_model=KnowledgeBaseOut)
async def update_knowledge_base(
    kb_id: uuid.UUID,
    payload: KnowledgeBaseUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeBaseOut:
    """Rename / re-describe / re-tune one knowledge base.

    ``model_fields_set`` is what makes this a correct PATCH: a key the client
    omitted stays as-is, while a key it sent as ``null`` is reset to "inherit
    the platform default". Reading ``payload.model_dump()`` without
    ``exclude_unset`` would erase every field the caller never mentioned.
    """
    kb = await _load_owned(db, kb_id, user)
    provided = payload.model_fields_set & set(_PATCHABLE)
    if not provided:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有需要更新的字段")

    swaps_model = (
        "embedding_model_id" in provided
        and payload.embedding_model_id != kb.embedding_model_id
    )
    if swaps_model:
        await _validate_embedding_model(db, payload.embedding_model_id)
    # Anything that changes how text becomes vectors: a new embedding space or a
    # new chunk shape.
    retunes_indexing = swaps_model or (
        "chunk_size" in provided and payload.chunk_size != kb.chunk_size
    ) or (
        "chunk_overlap" in provided and payload.chunk_overlap != kb.chunk_overlap
    )

    for field in provided:
        setattr(kb, field, getattr(payload, field))
    await db.commit()
    await db.refresh(kb)
    doc_counts, chunk_counts = await _counts(db, [kb.id])
    stale_chunks = chunk_counts.get(str(kb.id), 0)
    if retunes_indexing and stale_chunks:
        # 已入库的向量不会自动失效：它们仍是按旧模型/旧切分算出来的，状态却照旧
        # 显示 indexed。没有这条线索，之后的检索质量下降在日志里无从解释。
        logger.warning(
            "kb %s: embedding/chunking settings changed while %d chunk(s) were "
            "already indexed — those vectors are stale until re-indexed",
            kb.id,
            stale_chunks,
        )
    return _to_out(kb, doc_counts, chunk_counts)


async def _validate_embedding_model(db: AsyncSession, model_id: uuid.UUID | None) -> None:
    """A KB's collection holds one embedding space; refuse a model that can't fill it.

    Pointing a KB at a chat model (or a deleted id) used to be accepted and then
    turned *every* later retrieval into a silent zero-hit search.
    """
    if model_id is None:
        return
    cfg = await db.get(ModelConfig, model_id)
    if cfg is None or not cfg.is_embedding:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "embedding_model_id 必须指向一个存在的向量模型"
        )


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
