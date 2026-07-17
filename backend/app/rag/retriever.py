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
    ) -> list[SearchHit]:
        vectors = await self._embedder.embed([question])
        if not vectors:
            return []
        hits = await self._store.search(collection, vectors[0], top_k=top_k, filters=filters)
        if self._reranker is not None and hits:
            # Rerankers may want a wider recall window; pass top_k as the final cap.
            hits = await self._reranker.rerank(question, hits, top_k=top_k)
        return hits
