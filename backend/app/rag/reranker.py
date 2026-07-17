"""Rerankers.

``NoopReranker`` returns hits unchanged (score-order preserved). A real cross-encoder
reranker plugs in behind the same ``Reranker`` interface later — e.g. calling a
Cohere/Jina rerank API or a local bge-reranker — without touching RagService.
"""
from __future__ import annotations

from app.rag.base import Reranker, SearchHit


class NoopReranker(Reranker):
    """Pass-through reranker: preserves the vector store's score ordering."""

    async def rerank(self, query: str, hits: list[SearchHit], top_k: int = 5) -> list[SearchHit]:
        # Keep the strongest-scoring hits first; trim to top_k.
        ordered = sorted(hits, key=lambda h: h.score, reverse=True)
        return ordered[:top_k]
