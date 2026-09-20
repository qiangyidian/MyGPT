"""Document ingestion pipeline: upload -> parse -> split -> embed -> store.

``upload`` is what an HTTP request does; ``index_document`` is what the durable
queue calls (app/services/ingestion_queue.py), so nothing here runs in the
request that triggered it. ``index_document`` is idempotent for reindex (old
chunks + vectors for the same document are removed first). Every step sets a
coarse status on the Document row so the UI can show progress, and a failure
flips it to ``failed`` with an error message and returns an
:class:`~app.services.ingestion_queue.IngestionOutcome` instead of raising —
the queue decides whether that failure is worth another attempt.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from collections.abc import Sequence

from sqlalchemy import delete as _sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.core.file_signatures import (
    FileSignatureError,
    check_file_signature,
    verified_size,
)
from app.core.storage import UploadTooLargeError, get_storage
from app.models import Document, DocumentChunk, KnowledgeBase, ModelConfig
from app.providers.registry import get_provider_for_config
from app.rag.chunk_meta import annotate, chunk_metadata
from app.rag.embedder import ProviderEmbedder
from app.rag.parsers import default_parser
from app.rag.qdrant_store import get_vector_store
from app.rag.rag_service import collection_name
from app.rag.splitter import RecursiveTextSplitter
from app.services.ingestion_queue import IngestionOutcome

logger = logging.getLogger(__name__)

# Embedding batch size — most OpenAI-compatible endpoints cap a single request.
_EMBED_BATCH = 32

# Default page size for document listings; the API layer passes its own
# validated ``limit`` and this is the backstop for any other caller.
_DEFAULT_LIST_LIMIT = 200


async def _resolve_embedding_config(db: AsyncSession, kb: KnowledgeBase) -> ModelConfig:
    """KB's embedding model, else the first available embedding config."""
    if kb.embedding_model_id is not None:
        cfg = await db.get(ModelConfig, kb.embedding_model_id)
        if cfg is not None:
            return cfg
    result = await db.execute(
        select(ModelConfig)
        .where(ModelConfig.is_embedding.is_(True))
        .order_by(ModelConfig.created_at.asc())
        .limit(1)
    )
    cfg = result.scalar_one_or_none()
    if cfg is None:
        raise RuntimeError("No embedding model is configured")
    return cfg


async def upload(
    db: AsyncSession, kb: KnowledgeBase, user, upload_file
) -> Document:
    """Persist the uploaded file and create a pending Document row.

    Every check that guards *what may enter a knowledge base* lives here, because
    every path into a KB funnels through this function — the HTTP upload route and
    the attachment -> KB promotion both — so neither can skip one:

      * extension allow-list (``storage.save``, KB allow-list);
      * the real byte cap, enforced while the body streams to disk;
      * content-vs-extension sniffing with the SAME magic-byte table chat
        attachments use (an extension whitelist alone accepts any bytes named
        ``.pdf``);
      * the true stored size on ``Document.file_size``.

    A rejected file is removed from storage before raising, so nothing orphaned
    stays readable on disk.
    """
    settings = get_settings()
    storage = get_storage()
    filename = upload_file.filename or "upload"
    # Extension drives the parser; strip any path component.
    ext = os.path.splitext(filename)[1].lower()
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024

    try:
        path = await storage.save(upload_file, user.id, max_bytes=max_bytes)
    except UploadTooLargeError as exc:
        # storage.save deleted the partial object when it crossed the cap.
        raise AppException(
            413, "document_too_large", f"文件过大，最大 {settings.MAX_UPLOAD_MB}MB"
        ) from exc
    except ValueError as exc:
        # The allow-list reject in storage.save — reachable from save-to-KB, which
        # can carry an attachment extension the KB list does not allow (.png, …).
        raise AppException(400, "document_type_not_allowed", f"不支持的文件类型: {ext or '(无)'}") from exc

    try:
        check_file_signature(path, ext)
    except FileSignatureError as exc:
        try:
            await storage.delete(path)
        except Exception:  # pragma: no cover - cleanup is best effort
            logger.warning("could not remove rejected upload %s", path)
        code = "document_unreadable" if exc.reason == "unreadable" else "document_signature_mismatch"
        raise AppException(400, code, exc.message) from exc

    doc = Document(
        knowledge_base_id=kb.id,
        filename=filename,
        file_path=path,
        file_type=ext or ".txt",
        # Bytes actually written. This column used to be hard-coded 0, so every
        # size shown in the UI (and any limit reading it) saw an empty file.
        file_size=verified_size(path),
        status="pending",
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


async def _clear_existing(db: AsyncSession, doc: Document, collection: str) -> None:
    """Remove prior chunks + Qdrant points for this document (reindex safety)."""
    store = get_vector_store()
    try:
        await store.delete_by_filter(collection, {"document_id": str(doc.id)})
    except Exception as exc:
        logger.debug("delete_by_filter failed (ok on first index): %s", exc)
    # NB: ``_sa_delete`` — this module's own ``delete()`` service function below
    # shadows the SQLAlchemy construct in the module namespace.
    await db.execute(_sa_delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))


async def index_document(
    db: AsyncSession, document_id: uuid.UUID
) -> IngestionOutcome:
    """Full ingestion pipeline for one document. Never raises.

    The returned :class:`~app.services.ingestion_queue.IngestionOutcome` is how
    the queue learns what happened without this function propagating an
    exception into whichever background task called it. ``retryable`` is the
    split that matters: a provider outage should be retried, a file that has no
    parseable text should not be attempted four more times.
    """
    doc = await db.get(Document, document_id)
    if doc is None:
        return IngestionOutcome(ok=False, error="Document not found", retryable=False)
    kb = await db.get(KnowledgeBase, doc.knowledge_base_id)
    if kb is None:
        doc.status = "failed"
        doc.error_message = "Knowledge base not found"
        await db.commit()
        # A KB row does not come back; retrying would just re-check and fail.
        return IngestionOutcome(ok=False, error=doc.error_message, retryable=False)

    try:
        # 1. Parse.
        doc.status = "parsing"
        doc.error_message = None
        await db.commit()
        parsed = await asyncio.to_thread(default_parser.parse, doc.file_path, doc.file_type)
        text = parsed.text
        if not text or not text.strip():
            raise ValueError("文档内容为空或无法解析")

        # 2. Split. The KB may override the chunk shape; ``None`` keeps the
        # platform default (RecursiveTextSplitter falls back to settings).
        doc.status = "chunking"
        await db.commit()
        splitter = RecursiveTextSplitter(
            chunk_size=kb.chunk_size, chunk_overlap=kb.chunk_overlap
        )
        chunk_texts = splitter.split(text)
        if not chunk_texts:
            raise ValueError("切分后没有可用的文本块")
        token_counts = [splitter.count_tokens(c) for c in chunk_texts]
        # Recover each chunk's span/page/heading before the vectors exist: this is
        # the only moment the full parsed text is in hand.
        spans = annotate(chunk_texts, parsed)

        # 3. Embed (batched) + store.
        doc.status = "embedding"
        await db.commit()
        cfg = await _resolve_embedding_config(db, kb)
        provider = get_provider_for_config(cfg)
        embedder = ProviderEmbedder(provider, model=cfg.embedding_model_name)
        store = get_vector_store()
        collection = collection_name(kb.id)
        await store.ensure_collection(collection, embedder.dim)
        await _clear_existing(db, doc, collection)

        # Create chunk rows first so we have stable ids for the vector points.
        # ``embedding_model`` / ``embedding_dim`` are what make a later model
        # swap visible instead of a silent quality drop.
        digests = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in chunk_texts]
        chunk_rows = [
            DocumentChunk(
                document_id=doc.id,
                knowledge_base_id=kb.id,
                chunk_index=i,
                content=txt,
                token_count=tok,
                metadata_=chunk_metadata(
                    span, parsed, token_count=tok, sha256=digest
                ),
                embedding_model=cfg.embedding_model_name or None,
                embedding_dim=embedder.dim,
                content_sha256=digest,
            )
            for i, (txt, tok, span, digest) in enumerate(
                zip(chunk_texts, token_counts, spans, digests, strict=False)
            )
        ]
        db.add_all(chunk_rows)
        await db.flush()  # populate ids

        # Embed + upsert in batches.
        from app.rag.base import VectorPoint
        for start in range(0, len(chunk_rows), _EMBED_BATCH):
            batch = chunk_rows[start:start + _EMBED_BATCH]
            vectors = await embedder.embed([c.content for c in batch])
            if len(vectors) != len(batch):
                # An OpenAI-compatible endpoint can return fewer vectors than
                # inputs (e.g. when it skips items with a null embedding). zip()
                # would silently drop those chunks' Qdrant points while the doc
                # is still marked "indexed" with the full chunk_count — a silent
                # partial index. Fail loudly so the doc flips to "failed".
                raise RuntimeError(
                    f"embedding provider returned {len(vectors)} vectors "
                    f"for {len(batch)} chunks"
                )
            points = [
                VectorPoint(
                    id=str(c.id),
                    vector=vec,
                    payload={
                        "document_id": str(doc.id),
                        "document_name": doc.filename,
                        "chunk_id": str(c.id),
                        "chunk_index": c.chunk_index,
                        "text": c.content,
                        # Provenance the citation needs at query time, copied in
                        # now so retrieval never has to read the DB back.
                        "collection": collection,
                        "page": (c.metadata_ or {}).get("page"),
                        "heading": (c.metadata_ or {}).get("heading"),
                    },
                )
                for c, vec in zip(batch, vectors, strict=False)
            ]
            await store.upsert(collection, points)

        doc.status = "indexed"
        doc.chunk_count = len(chunk_rows)
        doc.error_message = None
        await db.commit()
        return IngestionOutcome(ok=True)
    except Exception as exc:
        logger.exception("indexing failed for document %s", document_id)
        doc.status = "failed"
        doc.error_message = str(exc)[:500]
        await db.commit()
        # ValueError = the parser/splitter saying "this text is not usable",
        # OSError = the stored file is gone or unreadable. Both are properties of
        # the row, so a retry would produce the identical failure.
        retryable = not isinstance(exc, (ValueError, OSError))
        return IngestionOutcome(
            ok=False, error=doc.error_message, retryable=retryable
        )


async def list_for_kb(
    db: AsyncSession,
    kb_id: uuid.UUID,
    *,
    limit: int = _DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> list[Document]:
    """Documents of one KB, newest first, capped at ``limit``.

    Paginated on purpose: a KB can hold thousands of rows and the listing used
    to be unbounded (and to eager-load every chunk body of every one of them).
    """
    stmt = (
        select(Document)
        .where(Document.knowledge_base_id == kb_id)
        .order_by(Document.created_at.desc())
        .offset(max(0, offset))
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get(db: AsyncSession, document_id: uuid.UUID) -> Document | None:
    return await db.get(Document, document_id)


async def delete_chunks_of(db: AsyncSession, document_ids: Sequence[uuid.UUID]) -> None:
    """Delete the chunk rows of the given documents (see ``purge_kb_rows``)."""
    ids = list(document_ids)
    if not ids:
        return
    await db.execute(_sa_delete(DocumentChunk).where(DocumentChunk.document_id.in_(ids)))


async def purge_kb_rows(db: AsyncSession, kb_ids: Sequence[uuid.UUID]) -> None:
    """Delete the chunk + document rows of the given KBs (plain SQL).

    Needed because ``KnowledgeBase.documents`` / ``Document.chunks`` are
    ``lazy="raise"`` now: the ORM can no longer load those collections to drive
    its delete-orphan cascade, and SQLite (CI + dev) does not enforce the FKs'
    ON DELETE CASCADE, so leaning on the database would silently leave rows (and
    a non-zero count in the UI) behind there.
    """
    ids = list(kb_ids)
    if not ids:
        return
    await db.execute(
        _sa_delete(DocumentChunk).where(DocumentChunk.knowledge_base_id.in_(ids))
    )
    await db.execute(_sa_delete(Document).where(Document.knowledge_base_id.in_(ids)))


async def delete(db: AsyncSession, document_id: uuid.UUID) -> bool:
    doc = await db.get(Document, document_id)
    if doc is None:
        return False
    collection = collection_name(doc.knowledge_base_id)
    try:
        await get_vector_store().delete_by_filter(collection, {"document_id": str(doc.id)})
    except Exception:
        pass
    # Also remove the stored file from disk.
    try:
        await get_storage().delete(doc.file_path)
    except Exception:
        pass
    # Chunks first, then the document row: the ORM cascade can no longer load
    # ``doc.chunks`` (lazy="raise"), and the DB cascade isn't guaranteed on
    # SQLite. See ``purge_kb_rows`` for the same reasoning on the KB side.
    await delete_chunks_of(db, [doc.id])
    await db.execute(_sa_delete(Document).where(Document.id == document_id))
    await db.commit()
    return True
