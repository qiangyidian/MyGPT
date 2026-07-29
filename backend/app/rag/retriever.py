"""Retriever: embed a query, search the vector store, optionally rerank.

A thin coordinator over Embedder + VectorStore + Reranker so RagService has one
call to make per retrieval. Stateless; constructed per request.
"""
from __future__ import annotations

from app.rag.base import Embedder, Reranker, SearchHit, VectorStore


class Retriever:
    def __init__(
        self,
        embedder: Embedder,
        store: VectorStore,
        reranker: Reranker | None = None,
    ) -> None:
        self._embedder = embedder
        self._store = store
        self._reranker = reranker

    async def retrieve(
        self,
        question: str,
        collection: str,
        top_k: int = 5,
        filters: dict | None = None,
        overfetch: int = 1,
    ) -> list[SearchHit]:
        vectors = await self._embedder.embed([question])
        if not vectors:
            return []
        # Over-fetch when a real reranker is present so it has a wider recall
        # window to re-order; the reranker trims back to top_k.
        fetch_k = max(top_k, top_k * max(1, overfetch))
        hits = await self._store.search(collection, vectors[0], top_k=fetch_k, filters=filters)
        if self._reranker is not None and hits:
            hits = await self._reranker.rerank(question, hits, top_k=top_k)
        else:
            hits = sorted(hits, key=lambda h: h.score, reverse=True)[:top_k]
        return hits
