"""Provider-backed embedder.

Wraps any ``ModelProvider`` (real OpenAI-compatible or Mock) so the RAG pipeline
gets embeddings through the same provider layer as chat — no parallel embedding code.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.providers.base import ModelProvider
from app.rag.base import Embedder


class ProviderEmbedder(Embedder):
    """Adapts a ModelProvider.embeddings() to the RAG Embedder interface."""

    def __init__(self, provider: ModelProvider, model: str | None = None) -> None:
        self._provider = provider
        self._model = model

    @property
    def dim(self) -> int:
        # Must equal the dimension the collection was created with and the value of
        # QDRANT_EMBEDDING_DIM. Real embedding models whose output dim differs must be
        # reflected here via the env var.
        return get_settings().QDRANT_EMBEDDING_DIM

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await self._provider.embeddings(texts, model=self._model)
