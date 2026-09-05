"""Qdrant vector store.

The default ``VectorStore`` implementation. The collection is created on demand with
cosine distance and the configured embedding dimension; if an existing collection's
dimension mismatches (e.g. you changed the embedding model), it is recreated so the
app self-heals instead of failing every upsert/search.

Point payload contract (shared with RagService / document_service):
    {document_id, document_name, chunk_id, chunk_index, text}
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.config import get_settings
from app.rag.base import SearchHit, VectorPoint, VectorStore

logger = logging.getLogger(__name__)


def _import_qdrant():
    """Lazy import so the module loads even if qdrant_client isn't installed."""
    from qdrant_client import (
        AsyncQdrantClient,  # type: ignore
        models,  # type: ignore
    )
    return AsyncQdrantClient, models


def _to_filter(filters: dict[str, Any] | None, models: Any):
    if not filters:
        return None
    conditions = []
    for key, value in filters.items():
        conditions.append(
            models.FieldCondition(key=key, match=models.MatchValue(value=str(value)))
        )
    return models.Filter(must=conditions) if conditions else None


class CollectionDimMismatchError(RuntimeError):
    """Raised when a collection's vector dim differs from the configured dim.

    Deliberately NOT auto-healed: recreating the collection deletes every
    stored vector (the whole knowledge base).
    """


class QdrantVectorStore(VectorStore):
    """Async Qdrant client wrapped behind the VectorStore interface."""

    def __init__(self) -> None:
        settings = get_settings()
        AsyncQdrantClient, _ = _import_qdrant()
        self._client = AsyncQdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY or None,
        )
        self._known: set[str] = set()

    async def ensure_collection(self, collection: str, dim: int) -> None:
        _, models = _import_qdrant()
        try:
            from qdrant_client.http.exceptions import UnexpectedResponse  # type: ignore
        except Exception:
            UnexpectedResponse = ()  # type: ignore
        try:
            info = await self._client.get_collection(collection_name=collection)
            # If the collection exists with a different dim, recreate it.
            existing_dim = (
                info.config.params.vectors.size
                if hasattr(info.config.params.vectors, "size")
                else getattr(info.config.params.vectors, "size", None)
            )
            if existing_dim and existing_dim != dim:
                # A dim mismatch means the embedding model changed. Silently
                # DELETING the collection ("self-heal") wiped the entire KB's
                # vectors — make it an explicit operator decision instead.
                if not get_settings().QDRANT_AUTO_RECREATE_ON_DIM_MISMATCH:
                    raise CollectionDimMismatchError(
                        f"collection {collection!r} has dim {existing_dim} but the "
                        f"configured embedding dim is {dim}. Re-index the knowledge "
                        "base (or point QDRANT_EMBEDDING_DIM/embedding model back to "
                        "the original), or set QDRANT_AUTO_RECREATE_ON_DIM_MISMATCH=true "
                        "to allow destructive recreation."
                    )
                logger.warning(
                    "Collection %s dim %s != required %s; recreating (operator opt-in)",
                    collection, existing_dim, dim,
                )
                # delete + create: recreate_collection is deprecated in
                # qdrant-client >=1.12 and will be removed.
                await self._client.delete_collection(collection_name=collection)
                await self._client.create_collection(
                    collection_name=collection,
                    vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
                )
            self._known.add(collection)
            return
        except UnexpectedResponse as exc:
            # Only a 404 means "collection missing" → fall through to create.
            # Other status codes (auth, 5xx, …) must NOT be masked as "missing".
            if getattr(exc, "status_code", None) != 404:
                logger.warning("qdrant get_collection failed for %s: %s", collection, exc)
                raise
        await self._client.create_collection(
            collection_name=collection,
            vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
        )
        self._known.add(collection)

    async def drop_collection(self, collection: str) -> None:
        """Remove an entire collection (KB deletion / account purge)."""
        await self._client.delete_collection(collection_name=collection)
        self._known.discard(collection)

    async def upsert(self, collection: str, points: list[VectorPoint]) -> None:
        _, models = _import_qdrant()
        await self._client.upsert(
            collection_name=collection,
            points=[
                models.PointStruct(id=p.id, vector=p.vector, payload=p.payload or {})
                for p in points
            ],
        )

    async def search(
        self, collection: str, query: list[float], top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        _, models = _import_qdrant()
        try:
            results = await self._client.search(
                collection_name=collection,
                query_vector=query,
                limit=top_k,
                query_filter=_to_filter(filters, models),
            )
        except Exception as exc:
            logger.warning("qdrant search failed on %s: %s", collection, exc)
            return []
        hits: list[SearchHit] = []
        for r in results:
            hits.append(SearchHit(id=str(r.id), score=float(r.score or 0.0), payload=dict(r.payload or {})))
        return hits

    async def delete_by_filter(self, collection: str, filters: dict[str, Any]) -> None:
        _, models = _import_qdrant()
        flt = _to_filter(filters, models)
        if flt is None:
            return
        await self._client.delete(
            collection_name=collection,
            points_selector=models.FilterSelector(filter=flt),
        )


_store: QdrantVectorStore | None = None


def get_vector_store() -> QdrantVectorStore:
    """Cached singleton — one Qdrant client per process."""
    global _store
    if _store is None:
        _store = QdrantVectorStore()
    return _store


async def close_vector_store() -> None:
    """Close + drop the cached client (called on app shutdown).

    Without this the AsyncQdrantClient's underlying httpx connection pool leaks
    on every worker reload / graceful shutdown.
    """
    global _store
    if _store is not None:
        try:
            await _store._client.close()
        except Exception:
            pass
        _store = None
