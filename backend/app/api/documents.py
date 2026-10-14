"""Document router: upload + list + delete + reindex.

Upload validates type/size, persists the file, creates a ``pending`` Document row,
and enqueues the ingestion pipeline (parse -> split -> embed -> Qdrant). The
Document row is the queue entry (see app/services/ingestion_queue.py), so the job
outlives the process that accepted the upload; waking the local worker is only a
latency shortcut. All access is ownership-scoped via the document's knowledge
base.
"""
from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import uuid

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.knowledge_bases import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.core.config import get_settings
from app.core.deps import get_current_user
from app.core.exceptions import AppException
from app.core.rate_limit import rate_limit_user
from app.db import get_db
from app.models import Document, DocumentChunk, KnowledgeBase, User
from app.schemas import (
    DocumentOut,
    DocumentPreview,
    LocatorOut,
    ReindexResult,
    UploadCapabilities,
)
from app.services import document_service
from app.services.ingestion_queue import enqueue as enqueue_ingestion
from app.services.ingestion_queue import notify_ingestion_worker

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


async def _queue_for_ingestion(db: AsyncSession, doc: Document) -> None:
    """Put the document in the durable queue, then nudge the local worker.

    The enqueue commits on the request's own session, so a document is never
    visible as ``pending`` without also being schedulable. Waking the worker is
    only a latency shortcut: any worker claims it on the next poll anyway.

    ``doc`` is re-read because the enqueue is a bulk UPDATE, and the identity-map
    synchronisation that comes with it expires the loaded row — a caller that
    then serialises it (the response model) would trigger a lazy load outside the
    event loop and die on ``MissingGreenlet``.

    ``False`` (the queue refused because another worker owns the row) is passed
    back for the caller to turn into its own error.
    """
    queued = await enqueue_ingestion(db, doc.id)
    await db.refresh(doc)
    notify_ingestion_worker()
    return queued


@router.get("/upload-capabilities", response_model=UploadCapabilities)
async def get_upload_capabilities(
    user: User = Depends(get_current_user),
) -> UploadCapabilities:
    """The effective KB-upload allow-list, straight from the code that enforces it.

    ``settings.allowed_extensions`` is the configured list intersected with the
    parser registry, so this is the only place a client can learn what will not
    be rejected — anything harder-coded in the UI is a snapshot of a moving rule.
    """
    settings = get_settings()
    return UploadCapabilities(
        allowed_extensions=sorted(settings.allowed_extensions),
        max_upload_mb=settings.MAX_UPLOAD_MB,
    )


@router.post(
    "/knowledge-bases/{kb_id}/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit_user(30, 60, "upload"))],
)
async def upload_document(
    kb_id: uuid.UUID,
    file: UploadFile,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DocumentOut:
    settings = get_settings()
    filename = file.filename or "upload"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in settings.allowed_extensions:
        raise HTTPException(BAD, f"不支持的文件类型: {ext or '(无)'}")
    # Deliberately no size check on ``file.size`` here: Starlette only populates it
    # once the body has already been received (and it can stay None, which skipped
    # the guard entirely). The real cap lives downstream —
    # ``app/services/document_service.py`` passes ``max_bytes`` into the storage
    # layer, which aborts + deletes the partial object the moment the stream
    # crosses it — together with the magic-byte content check.
    kb = await _load_owned_kb(db, kb_id, user)
    doc = await document_service.upload(db, kb, user, file)
    await _queue_for_ingestion(db, doc)
    item = DocumentOut.model_validate(doc)
    item.file_available = True  # 刚写完盘
    return item


@router.get("/knowledge-bases/{kb_id}/documents", response_model=list[DocumentOut])
async def list_documents(
    kb_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DocumentOut]:
    """One page of the KB's documents, newest first (see the KB list's cap)."""
    await _load_owned_kb(db, kb_id, user)
    docs = await document_service.list_for_kb(db, kb_id, limit=limit, offset=offset)
    out = []
    for d in docs:
        item = DocumentOut.model_validate(d)
        # 原件在不在磁盘（列表页只调一次 exists，量级 = 每页几十条）。False 时
        # 前端隐藏预览/下载入口——预览/下载接口本身会 404 code=storage_gone。
        item.file_available = bool(d.file_path) and os.path.exists(d.file_path)
        out.append(item)
    return out


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _load_owned_doc(db, document_id, user)
    await document_service.delete(db, document_id)


@router.post("/documents/{document_id}/reindex", response_model=ReindexResult)
async def reindex_document(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReindexResult:
    """Re-queue a document for ingestion and return the queue's snapshot of it.

    Reindex goes through the same durable enqueue as an upload, which is what
    resets the attempt counter and the backoff window: a document that failed
    because the embedding endpoint was down becomes indexable again the moment a
    human asks for it, while one that keeps failing still stops after
    ``INGEST_MAX_ATTEMPTS`` instead of looping forever.
    """
    doc = await _load_owned_doc(db, document_id, user)
    if not await _queue_for_ingestion(db, doc):
        # The queue refused: a worker holds a live lease, so indexing is running
        # right now. Queuing it again would hand the same file to a second worker.
        raise HTTPException(status.HTTP_409_CONFLICT, "文档正在索引中，请稍后再试")
    return ReindexResult(
        document_id=doc.id,
        status=doc.status,
        chunk_count=doc.chunk_count,
        ingest_attempts=doc.ingest_attempts,
        ingest_next_retry_at=doc.ingest_next_retry_at,
    )


# Extensions whose parsed text is (or likely is) Markdown source — render the
# preview with the Markdown renderer instead of a <pre> block. Structured
# formats below are re-rendered as GFM Markdown by preview_render (tables,
# page boundaries) so they also render richly.
_MD_LIKE_EXTS = {".md", ".markdown", ".mdx"}
_STRUCTURED_EXTS = {".pdf", ".csv", ".xlsx", ".xls", ".ods", ".docx", ".doc", ".odt", ".pptx", ".ppt", ".odp"}

# Hard cap per page so a pathological upload can't blow up a single JSON
# response; the client pages through the rest via ?offset=.
_PREVIEW_PAGE_CHARS = 200_000


@router.get("/documents/{document_id}/preview", response_model=DocumentPreview)
async def preview_document(
    document_id: uuid.UUID,
    offset: int = 0,
    limit: int = _PREVIEW_PAGE_CHARS,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DocumentPreview:
    """Online preview: the parsed full text of an ingested document.

    Reuses the SAME ingestion parser (pdf/docx/md/txt/…), so what the user
    previews is exactly what was chunked + embedded. Structured formats
    (pdf/office/csv) are re-rendered as GFM Markdown (real tables, page
    markers) from the SAME parse result. Long texts are paged: pass
    ``offset`` (and optionally ``limit`` ≤ the page cap) to fetch the
    remainder. The original file must still exist on disk; a missing file
    404s instead of returning stale text.
    """
    from app.rag.parsers import default_parser
    from app.rag.preview_render import render_preview_markdown

    doc = await _load_owned_doc(db, document_id, user)
    if not doc.file_path or not os.path.exists(doc.file_path):
        # 机器可读 code=storage_gone：前端据此显示「原件已被清理（检索索引仍可用）」
        # 并隐藏下载入口，而不是一个需要用户猜因由的笼统 404。
        raise AppException(
            status_code=NOT_FOUND,
            code="storage_gone",
            message="文档原件已不存在（数据库记录与检索索引仍在），无法预览",
        )
    if doc.status == "failed":
        raise HTTPException(BAD, f"文档解析失败：{doc.error_message or '未知错误'}")
    if doc.status in ("pending", "parsing", "chunking", "embedding"):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "文档正在解析中，请稍后再试"
        )
    try:
        parsed = await asyncio.to_thread(default_parser.parse, doc.file_path, doc.file_type)
    except ValueError as exc:
        raise HTTPException(BAD, f"该格式暂不支持预览: {exc}") from exc
    except Exception as exc:
        raise HTTPException(500, f"解析失败: {exc}") from exc

    ft = doc.file_type.lower()
    if ft in _STRUCTURED_EXTS:
        # Render structured formats as rich Markdown (tables, page markers)
        # from the same parse result the pipeline chunked.
        text = await asyncio.to_thread(render_preview_markdown, parsed, ft)
        render_as = "markdown"
    else:
        text = parsed.text or ""
        render_as = "markdown" if ft in _MD_LIKE_EXTS else "text"

    offset = max(0, offset)
    limit = max(1, min(limit, _PREVIEW_PAGE_CHARS))
    page = text[offset : offset + limit]
    return DocumentPreview(
        document_id=doc.id,
        filename=doc.filename,
        file_type=doc.file_type,
        file_size=doc.file_size or 0,
        status=doc.status or "indexed",
        render_as=render_as,
        chars=len(page),
        total_chars=len(text),
        truncated=offset + len(page) < len(text),
        content=page,
    )


@router.get("/documents/{document_id}/download")
async def download_document(
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """Stream the original upload back to the owner (preview → 下载原文件)."""
    doc = await _load_owned_doc(db, document_id, user)
    if not doc.file_path or not os.path.exists(doc.file_path):
        raise AppException(
            status_code=NOT_FOUND,
            code="storage_gone",
            message="文档原件已不存在（可能已被存储清理），无法下载",
        )

    media_type = mimetypes.guess_type(doc.filename)[0] or "application/octet-stream"
    return FileResponse(
        doc.file_path,
        media_type=media_type,
        filename=doc.filename,
    )


# ---------------------------------------------------------------------------
# 引用原文定位：chunk 在原件里的位置，以及它底下的那页图
# ---------------------------------------------------------------------------
#: 单页渲染的 dpi 上下限。上限存在的理由是内存：A4 在 300dpi 下已经是约
#: 2500×3500 的位图，再往上一次请求就能吃掉几百 MB。
_MIN_DPI, _MAX_DPI = 72, 300
#: 单页面积上限（pt²）。超出的页（工程图、超长海报）不渲染，避免拖垮进程。
_MAX_PAGE_AREA_PT = 2_000_000.0


def _render_page_png(path: str, page_no: int, dpi: int) -> bytes:
    """渲染一页为 PNG。**不带任何高亮** —— 框由 locator 单独给。

    这样图只依赖（文档, 页号, dpi），不依赖是哪个 chunk 被点开，才能长期缓存。
    """
    import pymupdf

    doc = pymupdf.open(path)
    try:
        if page_no < 1 or page_no > doc.page_count:
            raise IndexError(page_no)
        page = doc[page_no - 1]
        rect = page.rect
        if rect.width * rect.height > _MAX_PAGE_AREA_PT:
            raise ValueError("page too large to render")
        zoom = dpi / 72.0
        return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")
    finally:
        doc.close()


def _page_image_etag(doc: Document, page_no: int, dpi: int) -> str:
    """ETag = 原件指纹 + 页号 + dpi。

    指纹取文件本身的 (路径, 大小, mtime) 而不是 ``doc.id``：重新上传会换文档行，
    但**覆盖同一个 file_path**（重索引、存储迁移）不会 —— 那时 doc.id 不变而图
    已经变了，只用 id 做 ETag 会把旧图一直发给用户。
    """
    try:
        stat = os.stat(doc.file_path)
        fingerprint = f"{doc.file_path}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:  # pragma: no cover - 文件在检查与 stat 之间消失
        fingerprint = doc.file_path
    digest = hashlib.sha256(f"{fingerprint}:{page_no}:{dpi}".encode()).hexdigest()
    return f'"{digest}"'


@router.get("/documents/{document_id}/pages/{page_no}/image")
async def document_page_image(
    document_id: uuid.UUID,
    page_no: int,
    request: Request,
    dpi: int = Query(default=150, ge=_MIN_DPI, le=_MAX_DPI),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """渲染文档的某一页，作为引用定位的底图。"""
    doc = await _load_owned_doc(db, document_id, user)
    if not doc.file_path or not os.path.exists(doc.file_path):
        raise AppException(
            status_code=NOT_FOUND,
            code="storage_gone",
            message="文档原件已不存在，无法渲染原页",
        )

    etag = _page_image_etag(doc, page_no, dpi)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})

    try:
        payload = await asyncio.to_thread(_render_page_png, doc.file_path, page_no, dpi)
    except IndexError:
        raise HTTPException(NOT_FOUND, "页码超出范围") from None
    except Exception as exc:
        raise HTTPException(500, f"渲染失败: {exc}") from exc

    return Response(
        content=payload,
        media_type="image/png",
        # private：这是某个用户的文档，绝不能被共享缓存留存。
        headers={"ETag": etag, "Cache-Control": "private, max-age=86400"},
    )


def _locator_to_out(loc: object, document_id: uuid.UUID) -> LocatorOut:
    """Locator → 响应模型。四个分支对应四种 kind，前端按 kind 分派渲染器。"""
    kind = loc.kind  # type: ignore[attr-defined]
    if kind == "page_image":
        return LocatorOut(
            kind=kind,
            page=loc.page,  # type: ignore[attr-defined]
            total_pages=loc.total_pages,  # type: ignore[attr-defined]
            image_url=f"/api/documents/{document_id}/pages/{loc.page}/image",  # type: ignore[attr-defined]
            boxes=[[float(v) for v in box] for box in loc.boxes],  # type: ignore[attr-defined]
        )
    if kind == "sheet_range":
        return LocatorOut(
            kind=kind,
            sheet=loc.sheet,  # type: ignore[attr-defined]
            header=list(loc.header),  # type: ignore[attr-defined]
            rows=[list(r) for r in loc.rows],  # type: ignore[attr-defined]
            highlight=[loc.highlight[0], loc.highlight[1]],  # type: ignore[attr-defined]
        )
    if kind == "slide":
        return LocatorOut(
            kind=kind,
            slide_no=loc.slide_no,  # type: ignore[attr-defined]
            width=loc.width,  # type: ignore[attr-defined]
            height=loc.height,  # type: ignore[attr-defined]
            shapes=[
                {"rect": list(s.rect), "text": s.text, "hit": s.hit}
                for s in loc.shapes  # type: ignore[attr-defined]
            ],
        )
    return LocatorOut(
        kind=kind,
        text=loc.text,  # type: ignore[attr-defined]
        start=loc.start,  # type: ignore[attr-defined]
        end=loc.end,  # type: ignore[attr-defined]
    )


@router.get(
    "/documents/{document_id}/chunks/{chunk_id}/locator",
    response_model=LocatorOut,
)
async def locate_document_chunk(
    document_id: uuid.UUID,
    chunk_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LocatorOut:
    """一个 chunk 在原件里的位置。

    坐标是**请求时现算**的：用 chunk 库里已有的字符锚点，在刚刚重解析出的原件上
    定位。这样定位结果永远与当前解析器自洽，而坐标不必随解析器版本一起落库、
    也就不会静默过期（parsers.py 是高频改动的文件）。代价是一次重解析 —— 与
    ``/documents/{id}/preview`` 同一个取舍，那条路径早就在这么做了。
    """
    doc = await _load_owned_doc(db, document_id, user)
    chunk = await db.get(DocumentChunk, chunk_id)
    if chunk is None or chunk.document_id != doc.id:
        # 不区分"片段不存在"与"片段属于别的文档"：后者会变成他人在文档里的
        # 内容探测器（沿用 _load_owned_doc 的同款理由）。
        raise HTTPException(NOT_FOUND, "片段不存在")
    if not doc.file_path or not os.path.exists(doc.file_path):
        raise AppException(
            status_code=NOT_FOUND,
            code="storage_gone",
            message="文档原件已不存在，无法定位原文位置",
        )
    if doc.status in ("pending", "parsing", "chunking", "embedding"):
        raise HTTPException(status.HTTP_409_CONFLICT, "文档正在解析中，请稍后再试")

    from app.rag.locator import locate
    from app.rag.parsers import default_parser

    try:
        parsed = await asyncio.to_thread(
            default_parser.parse, doc.file_path, doc.file_type
        )
    except ValueError as exc:
        raise HTTPException(BAD, f"该格式暂不支持定位: {exc}") from exc
    except Exception as exc:
        raise HTTPException(500, f"解析失败: {exc}") from exc

    meta = chunk.metadata_ or {}
    loc = locate(
        parsed,
        page=meta.get("page"),
        char_start=meta.get("char_start"),
        char_end=meta.get("char_end"),
    )
    if loc is None:
        raise AppException(
            status_code=NOT_FOUND,
            code="no_locator",
            message=(
                "该文档无法定位到原文位置（原件格式不含坐标，"
                "或这个片段入库时没有记录位置）"
            ),
        )
    return _locator_to_out(loc, doc.id)
