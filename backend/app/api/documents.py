"""Document router: upload + list + delete + reindex.

Upload validates type/size, persists the file, creates a ``pending`` Document row,
and schedules the ingestion pipeline (parse -> split -> embed -> Qdrant) as a
background task with its own DB session. All access is ownership-scoped via the
document's knowledge base.
"""
from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_current_user
from app.db import AsyncSessionLocal, get_db
from app.models import Document, KnowledgeBase, User
from app.schemas import DocumentOut, ReindexResult
from app.services import document_service

router = APIRouter(prefix="/api", tags=["documents"])

NOT_FOUND = status.HTTP_404_NOT_FOUND
BAD = status.HTTP_400_BAD_REQUEST


async def _load_owned_kb(db: AsyncSession, kb_id: uuid.UUID, user: User) -> KnowledgeBase:
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(NOT_FOUND, "Knowledge base not found")
    if kb.user_id != user.id and user.role != "admin":
        raise HTTPException(NOT_FOUND, "Knowledge base not found")
    return kb


async def _load_owned_doc(db: AsyncSession, document_id: uuid.UUID, user: User) -> Document:
    doc = await db.get(Document, document_id)
    if doc is None:
        raise HTTPException(NOT_FOUND, "Document not found")
    kb = await db.get(KnowledgeBase, doc.knowledge_base_id)
    if kb is None or (kb.user_id != user.id and user.role != "admin"):
        raise HTTPException(NOT_FOUND, "Document not found")
    return doc


async def _index_background(document_id: uuid.UUID) -> None:
    """Run ingestion outside the request lifecycle, with a fresh session."""
    async with AsyncSessionLocal() as session:
        await document_service.index_document(session, document_id)


@router.post(
    "/knowledge-bases/{kb_id}/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    kb_id: uuid.UUID,
    file: UploadFile,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DocumentOut:
    settings = get_settings()
    filename = file.filename or "upload"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in settings.allowed_extensions:
        raise HTTPException(BAD, f"不支持的文件类型: {ext or '(无)'}")
    # Soft size guard — Starlette populates .size after the upload is received.
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(BAD, f"文件过大，最大 {settings.MAX_UPLOAD_MB}MB")

    kb = await _load_owned_kb(db, kb_id, user)
    doc = await document_service.upload(db, kb, user, file)
    background_tasks.add_task(_index_background, doc.id)
    return DocumentOut.model_validate(doc)


@router.get("/knowledge-bases/{kb_id}/documents", response_model=list[DocumentOut])
async def list_documents(
    kb_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DocumentOut]:
    await _load_owned_kb(db, kb_id, user)
    docs = await document_service.list_for_kb(db, kb_id)
    return [DocumentOut.model_validate(d) for d in docs]


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    doc = await _load_owned_doc(db, document_id, user)
    await db.refresh(doc)
    await document_service.delete(db, document_id)


@router.post("/documents/{document_id}/reindex", response_model=ReindexResult)
async def reindex_document(
    document_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReindexResult:
    """Kick off a reindex in the background; return the current snapshot."""
    doc = await _load_owned_doc(db, document_id, user)
    # Mark pending immediately so the UI reflects that work is (re)starting.
    doc.status = "pending"
    doc.error_message = None
    await db.commit()
    background_tasks.add_task(_index_background, document_id)
    return ReindexResult(document_id=doc.id, status="pending", chunk_count=doc.chunk_count)
