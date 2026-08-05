"""Document ingestion pipeline: upload -> parse -> split -> embed -> store.

``index_document`` runs the full RAG ingestion for one file and is meant to be called
as a background task after upload. It is idempotent for reindex (old chunks + vectors
for the same document are removed first). Every step sets a coarse status on the
Document row so the UI can show progress, and any failure flips it to ``failed`` with
an error message rather than raising.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.storage import get_storage
from app.models import Document, DocumentChunk, KnowledgeBase, ModelConfig
from app.providers.registry import get_provider_for_config
from app.rag.embedder import ProviderEmbedder
from app.rag.parsers import default_parser
from app.rag.qdrant_store import get_vector_store
from app.rag.rag_service import collection_name
from app.rag.splitter import RecursiveTextSplitter
from app.schemas import ReindexResult

logger = logging.getLogger(__name__)

# Embedding batch size — most OpenAI-compatible endpoints cap a single request.
_EMBED_BATCH = 32


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
    """Persist the uploaded file and create a pending Document row."""
    storage = get_storage()
    path = await storage.save(upload_file, user.id)
    filename = upload_file.filename or "upload"
    # Extension drives the parser; strip any path component.
    import os
    ext = os.path.splitext(filename)[1].lower()
    doc = Document(
        knowledge_base_id=kb.id,
        filename=filename,
        file_path=path,
        file_type=ext or ".txt",
        file_size=0,
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
    except Exception as exc:  # noqa: BLE001
        logger.debug("delete_by_filter failed (ok on first index): %s", exc)
    await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))


async def index_document(db: AsyncSession, document_id: uuid.UUID) -> None:
    """Full ingestion pipeline for one document. Never raises; sets status."""
    doc = await db.get(Document, document_id)
    if doc is None:
        return
    kb = await db.get(KnowledgeBase, doc.knowledge_base_id)
    if kb is None:
        doc.status = "failed"
        doc.error_message = "Knowledge base not found"
        await db.commit()
        return

    try:
        # 1. Parse.
        doc.status = "parsing"
        doc.error_message = None
        await db.commit()
        parsed = await asyncio.to_thread(default_parser.parse, doc.file_path, doc.file_type)
        text = parsed.text
        if not text or not text.strip():
            raise ValueError("文档内容为空或无法解析")

        # 2. Split.
        doc.status = "chunking"
        await db.commit()
        splitter = RecursiveTextSplitter()
        chunk_texts = splitter.split(text)
        if not chunk_texts:
            raise ValueError("切分后没有可用的文本块")
        token_counts = [splitter.count_tokens(c) for c in chunk_texts]

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
        chunk_rows = [
            DocumentChunk(
                document_id=doc.id,
                knowledge_base_id=kb.id,
                chunk_index=i,
                content=txt,
                token_count=tok,
                metadata_={},
            )
            for i, (txt, tok) in enumerate(zip(chunk_texts, token_counts))
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
                    },
                )
                for c, vec in zip(batch, vectors)
            ]
            await store.upsert(collection, points)

        doc.status = "indexed"
        doc.chunk_count = len(chunk_rows)
        doc.error_message = None
        await db.commit()
    except Exception as exc:  # noqa: BLE001 — surface failure on the row
        logger.exception("indexing failed for document %s", document_id)
        doc.status = "failed"
        doc.error_message = str(exc)[:500]
        await db.commit()


async def reindex(db: AsyncSession, document_id: uuid.UUID) -> ReindexResult:
    """Re-run ingestion for a document (clears old chunks + vectors first)."""
    doc = await db.get(Document, document_id)
    if doc is None:
        return ReindexResult(document_id=document_id, status="not_found", chunk_count=0)
    await index_document(db, document_id)
    await db.refresh(doc)
    return ReindexResult(
        document_id=doc.id, status=doc.status, chunk_count=doc.chunk_count
    )


async def list_for_kb(db: AsyncSession, kb_id: uuid.UUID) -> list[Document]:
    result = await db.execute(
        select(Document).where(Document.knowledge_base_id == kb_id).order_by(Document.created_at.desc())
    )
    return list(result.scalars().all())


async def get(db: AsyncSession, document_id: uuid.UUID) -> Optional[Document]:
    return await db.get(Document, document_id)


async def delete(db: AsyncSession, document_id: uuid.UUID) -> bool:
    doc = await db.get(Document, document_id)
    if doc is None:
        return False
    collection = collection_name(doc.knowledge_base_id)
    try:
        get_vector_store().delete_by_filter(collection, {"document_id": str(doc.id)})
    except Exception:  # noqa: BLE001
        pass
    # Also remove the stored file from disk.
    try:
        await get_storage().delete(doc.file_path)
    except Exception:  # noqa: BLE001
        pass
    await db.delete(doc)
    await db.commit()
    return True
