"""Document router: upload + list + delete + reindex.

Upload validates type/size, persists the file, creates a ``pending`` Document row,
and schedules the ingestion pipeline (parse -> split -> embed -> Qdrant) as a
background task with its own DB session. All access is ownership-scoped via the
document's knowledge base.
"""
from __future__ import annotations

import asyncio
import os
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_current_user
from app.core.rate_limit import rate_limit_user
from app.db import AsyncSessionLocal, get_db
from app.models import Document, KnowledgeBase, User
from app.schemas import DocumentOut, DocumentPreview, ReindexResult
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
    dependencies=[Depends(rate_limit_user(30, 60, "upload"))],
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


# Extensions whose parsed text is (or likely is) Markdown source — render the
# preview with the Markdown renderer instead of a <pre> block.
_MD_LIKE_EXTS = {".md", ".markdown", ".mdx"}

# Hard cap so a pathological upload can't blow up the JSON response.
_PREVIEW_MAX_CHARS = 500_000


@router.get("/documents/{document_id}/preview", response_model=DocumentPreview)
async def preview_document(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DocumentPreview:
    """Online preview: the parsed full text of an ingested document.

    Reuses the SAME ingestion parser (pdf/docx/md/txt/…), so what the user
    previews is exactly what was chunked + embedded. The original file must
    still exist on disk; a missing file 404s instead of returning stale text.
    """
    from app.rag.parsers import default_parser

    doc = await _load_owned_doc(db, document_id, user)
    if not doc.file_path or not os.path.exists(doc.file_path):
        raise HTTPException(NOT_FOUND, "原始文件不存在或已被清理，无法预览")
    try:
        parsed = await asyncio.to_thread(default_parser.parse, doc.file_path, doc.file_type)
    except ValueError as exc:
        raise HTTPException(BAD, f"该格式暂不支持预览: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — parser failures are user-facing
        raise HTTPException(500, f"解析失败: {exc}") from exc

    text = parsed.text or ""
    truncated = len(text) > _PREVIEW_MAX_CHARS
    content = text[:_PREVIEW_MAX_CHARS]
    render_as = "markdown" if doc.file_type.lower() in _MD_LIKE_EXTS else "text"
    return DocumentPreview(
        document_id=doc.id,
        filename=doc.filename,
        file_type=doc.file_type,
        render_as=render_as,
        chars=len(content),
        content=content + ("\n\n…（内容过长，已截断）" if truncated else ""),
    )
