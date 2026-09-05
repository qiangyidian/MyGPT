"""Per-attachment ephemeral RAG for oversized documents.

When an attachment's extracted text is too large to inline into the prompt, we
chunk + embed it into a shared ``chat_attachments`` Qdrant collection (points
tagged with ``attachment_id``) and, at send time, retrieve only the chunks
relevant to the user's question. This is the "smart hybrid" large-file path;
small documents skip it entirely and are inlined.

Everything here is **best-effort**: if no embedding model is configured, Qdrant
is unreachable, or any step raises, callers fall back to inline truncation, so
the chat never breaks. Indexing runs in the background parse task so the send
path pays only the (fast) retrieval cost.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModelConfig
from app.providers.registry import get_provider_for_config
from app.rag.base import VectorPoint
from app.rag.embedder import ProviderEmbedder
from app.rag.qdrant_store import get_vector_store
from app.rag.splitter import RecursiveTextSplitter

logger = logging.getLogger(__name__)

# One shared collection; per-attachment isolation via payload filter. Distinct
# from KB collections (kb_<id>), so no collision.
_COLLECTION = "chat_attachments"
_EMBED_BATCH = 32
# Parse-time gate: pre-index docs larger than this (covers any plausible model
# budget). The bind-time decision is per-model; this just ensures the index is
# ready by send time for genuinely large documents.
_INDEX_THRESHOLD_CHARS = 12000


async def _resolve_embedder(db: AsyncSession) -> ProviderEmbedder | None:
    """Build an embedder from the first available embedding ModelConfig, or None."""
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


async def ensure_index(db: AsyncSession, attachment_id: uuid.UUID, text: str) -> bool:
    """Chunk + embed + upsert one attachment's text. Idempotent. Best-effort.

    Returns True if the index was (re)built; False if skipped or failed.
    """
    text = (text or "").strip()
    if not text:
        return False
    try:
        embedder = await _resolve_embedder(db)
        if embedder is None:
            return False
        splitter = RecursiveTextSplitter()
        chunks = splitter.split(text)
        if not chunks:
            return False
        store = get_vector_store()
        await store.ensure_collection(_COLLECTION, embedder.dim)
        # Idempotency: clear any prior points for this attachment first.
        await store.delete_by_filter(_COLLECTION, {"attachment_id": str(attachment_id)})
        aid = str(attachment_id)
        for start in range(0, len(chunks), _EMBED_BATCH):
            batch = chunks[start:start + _EMBED_BATCH]
            vectors = await embedder.embed(batch)
            if len(vectors) != len(batch):
                logger.warning(
                    "attachment index: embedder returned %d/<%d vectors, skipping batch",
                    len(vectors), len(batch),
                )
                continue
            points = [
                VectorPoint(
                    id=uuid.uuid4().hex,
                    vector=vec,
                    payload={"attachment_id": aid, "chunk_index": start + i, "text": txt},
                )
                for i, (txt, vec) in enumerate(zip(batch, vectors, strict=False))
            ]
            await store.upsert(_COLLECTION, points)
        return True
    except Exception as exc:
        logger.warning("attachment RAG index failed for %s: %s", attachment_id, exc)
        return False


async def retrieve(
    db: AsyncSession, attachment_id: uuid.UUID, query: str, top_k: int = 5
) -> list[str]:
    """Top-k relevant chunk texts for ``query`` within one attachment. [] on failure."""
    query = (query or "").strip()
    if not query:
        return []
    try:
        embedder = await _resolve_embedder(db)
        if embedder is None:
            return []
        qvec = (await embedder.embed([query]))[0]
        hits = await get_vector_store().search(
            _COLLECTION, qvec, top_k=top_k, filters={"attachment_id": str(attachment_id)}
        )
        return [h.payload.get("text", "") for h in hits if h.payload.get("text")]
    except Exception as exc:
        logger.warning("attachment RAG retrieve failed for %s: %s", attachment_id, exc)
        return []


async def drop(attachment_id: uuid.UUID) -> None:
    """Remove all indexed chunks for an attachment (on delete). Best-effort."""
    try:
        await get_vector_store().delete_by_filter(
            _COLLECTION, {"attachment_id": str(attachment_id)}
        )
    except Exception as exc:
        logger.debug("attachment RAG drop failed for %s: %s", attachment_id, exc)


def should_index(text: str) -> bool:
    """Parse-time gate: is this doc large enough to be worth pre-indexing?"""
    return len((text or "").strip()) > _INDEX_THRESHOLD_CHARS
