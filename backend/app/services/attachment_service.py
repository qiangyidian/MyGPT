"""Chat attachment lifecycle: upload (validate) -> background parse -> bind on
send -> optional save-to-KB.

Distinct from the long-lived KnowledgeBase document pipeline
(``document_service``): attachments are bound to a conversation/message, default
temporary, and never auto-indexed into a shared KB. File bytes live in the
storage backend (``core.storage``); only the metadata row is persisted here.

Security:
  * Extension + MIME + magic-byte signature are all checked (extension alone is
    not trusted).
  * Filenames are sanitized to a basename (no path traversal).
  * Per-file size and per-message count are capped by settings.
  * Every read/write is ownership-scoped (``user_id`` + ``conversation_id``).
  * The stored path/key is never returned to the client (only an opaque id).
  * A parse failure on one attachment never affects the others or the chat.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.core.storage import get_storage
from app.db import AsyncSessionLocal
from app.models import ChatAttachment, KnowledgeBase, User
from app.rag.parsers import default_parser

logger = logging.getLogger(__name__)

# Strong references to fire-and-forget background tasks, so the event loop's
# weak reference cannot GC them mid-flight (see asyncio.create_task docs).
_BACKGROUND_TASKS: set[asyncio.Task] = set()

# Parsing (pdfplumber / pandas / ...) is CPU-bound and can hang on pathological
# files. Run it on a dedicated, bounded pool so a stuck parse can never starve
# the shared default executor used by reranking, KB indexing, and tool calls.
_PARSE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="attachment-parse"
)


def _spawn(coro):
    """Schedule a background coroutine and keep a strong reference to it."""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


# Extension -> (accepted MIME prefixes, magic-byte signatures). Signatures are
# matched against the first bytes of the saved file. Text types have no robust
# signature and are accepted on extension + MIME alone.
_EXT_RULES: dict[str, tuple[tuple[str, ...], tuple[bytes, ...]]] = {
    ".pdf":  (("application/pdf",), (b"%PDF",)),
    ".png":  (("image/png",), (b"\x89PNG\r\n\x1a\n",)),
    ".jpg":  (("image/jpeg",), (b"\xff\xd8\xff",)),
    ".jpeg": (("image/jpeg",), (b"\xff\xd8\xff",)),
    ".webp": (("image/webp", "image/riff"), (b"RIFF",)),
    ".docx": (("application/vnd.openxmlformats-officedocument.wordprocessingml.document",
               "application/zip", "application/octet-stream"), (b"PK\x03\x04",)),
    ".xlsx": (("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
               "application/zip", "application/octet-stream"), (b"PK\x03\x04",)),
    ".txt":  (("text/plain", "text/markdown", "application/octet-stream"), ()),
    ".md":   (("text/markdown", "text/plain", "application/octet-stream"), ()),
    ".csv":  (("text/csv", "text/plain", "application/vnd.ms-excel", "application/octet-stream"), ()),
    ".json": (("application/json", "text/plain", "application/octet-stream"), ()),
}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_TABLE_EXTS = {".csv", ".xlsx"}


def _sanitize_filename(name: str) -> str:
    """Reduce a client-supplied filename to a safe basename."""
    base = os.path.basename(name or "") or "attachment"
    # Drop any null bytes / control chars and trim length.
    base = base.replace("\x00", "").strip()
    return base[:180] or "attachment"


def _ext_of(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def _validate_extension(filename: str, content_type: str | None) -> str:
    settings = get_settings()
    ext = _ext_of(filename)
    allowed = {e.strip().lower() for e in settings.ATTACHMENT_ALLOWED_EXT.split(",") if e.strip()}
    if ext not in allowed:
        raise AppException(400, "attachment_type_not_allowed", f"不支持的文件类型: {ext or '(无)'}")
    rules = _EXT_RULES.get(ext)
    if rules is not None:
        accepted_mimes, _sigs = rules
        # MIME check is advisory for octet-stream (browsers often send generic);
        # only reject a clearly contradictory declared MIME.
        if content_type and content_type != "application/octet-stream":
            ct = content_type.lower().split(";")[0].strip()
            if accepted_mimes and not any(ct == m or ct.startswith(m) for m in accepted_mimes):
                # Permit text/* for text-ish extensions even if browser is picky.
                if not (ct.startswith("text/") and ext in {".txt", ".md", ".csv", ".json"}):
                    raise AppException(400, "attachment_mime_mismatch",
                                       f"文件扩展名 {ext} 与类型 {ct} 不一致")
    return ext


def _verify_signature(path: str, ext: str) -> None:
    """Compare the file's leading bytes against the known signature for ext."""
    rules = _EXT_RULES.get(ext)
    if rules is None:
        return
    _accepted, sigs = rules
    if not sigs:
        return
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError as exc:  # pragma: no cover
        raise AppException(400, "attachment_unreadable", "无法读取上传文件") from exc
    if not any(head.startswith(s) for s in sigs):
        raise AppException(400, "attachment_signature_mismatch",
                           "文件内容与其扩展名声明不一致")


async def _count_unbound(db: AsyncSession, conversation_id: uuid.UUID) -> int:
    res = await db.execute(
        select(ChatAttachment.id)
        .where(
            ChatAttachment.conversation_id == conversation_id,
            ChatAttachment.message_id.is_(None),
            ChatAttachment.status != "deleted",
        )
    )
    return len(res.all())


async def upload(
    db: AsyncSession,
    *,
    user: User,
    conversation_id: uuid.UUID,
    upload_file: Any,
) -> ChatAttachment:
    """Validate + persist one upload. Returns the created attachment row.

    Parsing runs in the background (in its own session) so the upload responds
    fast and one parse failure never blocks another.
    """
    settings = get_settings()
    # Conversation ownership is enforced by the router before calling here.
    unbound = await _count_unbound(db, conversation_id)
    if unbound >= settings.MAX_ATTACHMENTS_PER_MESSAGE:
        raise AppException(400, "attachment_limit",
                           f"单条消息最多 {settings.MAX_ATTACHMENTS_PER_MESSAGE} 个附件")

    original = _sanitize_filename(upload_file.filename or "attachment")
    ext = _validate_extension(original, getattr(upload_file, "content_type", None))

    # Size guard: read in a bounded loop without trusting content-length.
    max_bytes = settings.ATTACHMENT_MAX_MB * 1024 * 1024
    size = 0
    await upload_file.seek(0)
    # We must persist the file to inspect it; stream-copy with a running tally
    # and abort if it exceeds the cap.
    storage = get_storage()
    # Attachments accept a broader allow-list than KB uploads (e.g. images).
    attachment_allow = {
        e.strip().lower() for e in settings.ATTACHMENT_ALLOWED_EXT.split(",") if e.strip()
    }
    # storage.save streams to disk in 1MB chunks but does not enforce our cap;
    # so we verify size after save and reject+delete if too large.
    storage_key = await storage.save(upload_file, user.id, allowed_extensions=attachment_allow)
    try:
        size = os.path.getsize(storage_key)
    except OSError:
        size = 0

    def _cleanup() -> None:
        try:
            asyncio.get_event_loop().create_task(storage.delete(storage_key))
        except Exception:  # noqa: BLE001
            pass

    if size > max_bytes:
        await storage.delete(storage_key)
        raise AppException(413, "attachment_too_large",
                           f"单个附件不能超过 {settings.ATTACHMENT_MAX_MB}MB")

    try:
        _verify_signature(storage_key, ext)
    except AppException:
        # Don't leave an rejected file on disk.
        try:
            await storage.delete(storage_key)
        except Exception:  # noqa: BLE001
            pass
        raise

    attachment = ChatAttachment(
        user_id=user.id,
        conversation_id=conversation_id,
        filename=os.path.basename(storage_key),
        original_filename=original,
        mime_type=getattr(upload_file, "content_type", "") or "",
        size_bytes=size,
        storage_key=storage_key,
        status="uploaded",
        parse_status="skipped" if ext in _IMAGE_EXTS else "pending",
        is_temporary=True,
    )
    db.add(attachment)
    await db.commit()
    await db.refresh(attachment)

    if ext not in _IMAGE_EXTS:
        _spawn(_parse_attachment_bg(attachment.id))

    return attachment


async def _parse_attachment_bg(attachment_id: uuid.UUID) -> None:
    """Background parse with its own DB session + timeout. Never raises out.

    Runs the (sync, potentially heavy) parser off the event loop. Any failure
    flips the row to ``failed`` with an error message; sibling attachments are
    unaffected because each has its own task.
    """
    settings = get_settings()
    try:
        async with AsyncSessionLocal() as db:
            att = await db.get(ChatAttachment, attachment_id)
            if att is None:
                return
            att.status = "parsing"
            att.parse_status = "parsing"
            await db.commit()
            try:
                text, preview = await asyncio.wait_for(
                    _extract(att.storage_key, att.original_filename), settings.ATTACHMENT_PARSE_TIMEOUT
                )
            except asyncio.TimeoutError:
                att.parse_status = "failed"
                att.status = "failed"
                att.error_message = "解析超时"
                await db.commit()
                return
            except Exception as exc:  # noqa: BLE001
                att.parse_status = "failed"
                att.status = "failed"
                att.error_message = str(exc)[:500]
                await db.commit()
                logger.warning("attachment %s parse failed: %s", attachment_id, exc)
                return
            att.extracted_text = text
            att.preview_metadata = preview
            att.parse_status = "ready"
            att.status = "ready"
            await db.commit()
    except Exception:  # noqa: BLE001 — background; never propagate
        logger.exception("attachment parse task crashed for %s", attachment_id)


async def _extract(storage_key: str, filename: str) -> tuple[str, dict[str, Any]]:
    """Parse text + collect preview hints, off the event loop."""
    ext = _ext_of(filename)
    storage = get_storage()

    def _work() -> tuple[str, dict[str, Any]]:
        preview: dict[str, Any] = {}
        if ext in _IMAGE_EXTS:
            return "", preview
        with storage.open(storage_key) as fh:
            tmp_path = fh.name  # LocalStorage returns a real file handle
        text = default_parser.parse(tmp_path, ext)
        preview = _preview_for(tmp_path, ext, text)
        return text, preview

    loop = asyncio.get_running_loop()
    # Run on the dedicated parse pool, NOT the shared default executor, so a
    # pathological file that hangs the parser cannot starve reranking/tools.
    return await loop.run_in_executor(_PARSE_EXECUTOR, _work)


def _preview_for(path: str, ext: str, text: str) -> dict[str, Any]:
    """Best-effort structured preview (page count, rows/cols, sheet names...)."""
    preview: dict[str, Any] = {}
    try:
        if ext == ".pdf":
            import pdfplumber  # type: ignore
            with pdfplumber.open(path) as pdf:
                preview = {"pages": len(pdf.pages)}
        elif ext == ".csv":
            import pandas as pd  # type: ignore
            df = pd.read_csv(path, nrows=0)
            preview = {"format": "csv", "columns": list(df.columns)}
        elif ext == ".xlsx":
            import pandas as pd  # type: ignore
            xl = pd.ExcelFile(path)
            preview = {"format": "xlsx", "sheets": xl.sheet_names}
        elif ext in {".txt", ".md", ".json"}:
            preview = {"chars": len(text or "")}
    except Exception:  # noqa: BLE001 — preview is best-effort
        pass
    return preview


async def resolve_and_bind_attachments(
    db: AsyncSession,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    attachment_ids: list[uuid.UUID],
) -> tuple[list[dict[str, Any]], str]:
    """Ownership-check + bind attachments to a message; return (summaries, text).

    Every id must exist and belong to (user_id, conversation_id) or the turn is
    rejected. Parsed text from ready attachments is concatenated for inline
    context injection.
    """
    if not attachment_ids:
        return [], ""
    settings = get_settings()
    if len(attachment_ids) > settings.MAX_ATTACHMENTS_PER_MESSAGE:
        raise AppException(400, "attachment_limit",
                           f"单条消息最多 {settings.MAX_ATTACHMENTS_PER_MESSAGE} 个附件")

    res = await db.execute(
        select(ChatAttachment).where(ChatAttachment.id.in_(list(attachment_ids)))
    )
    rows = list(res.scalars().all())
    if len(rows) != len(set(attachment_ids)):
        raise AppException(404, "attachment_not_found", "部分附件不存在")
    # Stable order matching the client request.
    by_id = {r.id: r for r in rows}
    ordered = [by_id[i] for i in attachment_ids if i in by_id]

    summaries: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for att in ordered:
        if att.user_id != user_id or att.conversation_id != conversation_id:
            raise AppException(403, "forbidden", "无权访问该附件")
        att.message_id = message_id
        summaries.append({
            "id": str(att.id),
            "filename": att.original_filename,
            "mime_type": att.mime_type,
            "size_bytes": att.size_bytes,
            "status": att.status,
            "parse_status": att.parse_status,
        })
        if att.extracted_text:
            text_parts.append(f"[附件: {att.original_filename}]\n{att.extracted_text}")
    await db.flush()
    return summaries, "\n\n".join(text_parts)


async def get_owned(db: AsyncSession, attachment_id: uuid.UUID, user_id: uuid.UUID) -> ChatAttachment:
    att = await db.get(ChatAttachment, attachment_id)
    if att is None or att.user_id != user_id or att.status == "deleted":
        raise AppException(404, "attachment_not_found", "附件不存在")
    return att


async def open_bytes(att: ChatAttachment) -> bytes:
    """Read the stored file bytes (ownership already verified by caller)."""
    storage = get_storage()
    def _read() -> bytes:
        with storage.open(att.storage_key) as fh:
            return fh.read()
    return await asyncio.to_thread(_read)


async def delete(db: AsyncSession, attachment_id: uuid.UUID, user_id: uuid.UUID) -> None:
    att = await get_owned(db, attachment_id, user_id)
    try:
        await get_storage().delete(att.storage_key)
    except Exception:  # noqa: BLE001
        pass
    att.status = "deleted"
    await db.commit()


async def list_for_conversation(
    db: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> list[ChatAttachment]:
    res = await db.execute(
        select(ChatAttachment)
        .where(
            ChatAttachment.conversation_id == conversation_id,
            ChatAttachment.user_id == user_id,
            ChatAttachment.status != "deleted",
        )
        .order_by(ChatAttachment.created_at.asc())
    )
    return list(res.scalars().all())


async def save_to_kb(
    db: AsyncSession, attachment_id: uuid.UUID, user_id: uuid.UUID, kb_id: uuid.UUID
) -> ChatAttachment:
    """Promote a temporary attachment into a long-lived KB document.

    Reuses the document ingestion pipeline so the file is parsed, chunked,
    embedded and indexed like any KB upload. The attachment row keeps a pointer
    to the KB for traceability.
    """
    att = await get_owned(db, attachment_id, user_id)
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or kb.user_id != user_id:
        raise AppException(404, "knowledge_base_not_found", "知识库不存在")

    from app.services import document_service

    # Re-open the stored bytes as an UploadFile-shaped object the document
    # pipeline expects.
    data = await open_bytes(att)
    import io
    from fastapi import UploadFile

    upload_file = UploadFile(filename=att.original_filename, file=io.BytesIO(data))
    # document_service.upload creates + commits + returns the new Document row;
    # use its id directly. A filename-based re-query would race on duplicate
    # names (one doc double-indexed, another never indexed).
    user_obj = await db.get(User, user_id)
    doc = await document_service.upload(db, kb, user_obj, upload_file)
    _spawn(_index_document_safe(doc.id))
    att.knowledge_base_id = kb_id
    att.is_temporary = False
    await db.commit()
    await db.refresh(att)
    return att


async def _index_document_safe(document_id: uuid.UUID) -> None:
    """Background index with its own session; never raises."""
    from app.services import document_service
    try:
        async with AsyncSessionLocal() as db:
            await document_service.index_document(db, document_id)
    except Exception:  # noqa: BLE001
        logger.exception("save-to-kb indexing failed for document %s", document_id)
