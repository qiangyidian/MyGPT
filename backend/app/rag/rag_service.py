"""RagService — the single entry point for knowledge-base retrieval.

ChatService calls ``rag_service.retrieve(db, question, kb_id, top_k)`` and gets back
a context string (ready to splice into the system prompt) plus a list of citations
for the UI. Everything below this (embedder, store, reranker) is an implementation
detail business code never touches.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import KnowledgeBase, ModelConfig
from app.providers.registry import get_provider_for_config
from app.rag.embedder import ProviderEmbedder
from app.rag.prompts import build_rag_context, format_context_block
from app.rag.qdrant_store import get_vector_store
from app.rag.reranker import NoopReranker
from app.rag.retriever import Retriever
from app.schemas import Citation

logger = logging.getLogger(__name__)


def collection_name(kb_id: uuid.UUID | str) -> str:
    """Stable Qdrant collection name for a knowledge base.

    Kept here (and imported by document_service) so indexing and retrieval always
    agree on the same collection.
    """
    return "kb_" + str(kb_id).replace("-", "")


async def _resolve_embedding_config(db: AsyncSession, kb: KnowledgeBase) -> ModelConfig:
    """Pick the embedding ModelConfig: the KB's own, else any system/user embedding config."""
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


class RagService:
    """Orchestrates embed -> search -> (rerank) -> context+citations."""

    async def retrieve(
        self,
        db: AsyncSession,
        question: str,
        kb_id: uuid.UUID | str,
        top_k: int | None = None,
    ) -> tuple[str, list[Citation]]:
        """Return (rag_context, citations) for the question against the given KB.

        Best-effort: any failure (no KB, no collection yet, store error) returns
        ("", []) so chat never breaks because of RAG.
        """
        if not question or not str(question).strip():
            return "", []
        settings = get_settings()
        top_k = top_k or settings.RAG_TOP_K

        kb = await db.get(KnowledgeBase, uuid.UUID(str(kb_id)))
        if kb is None:
            return "", []

        try:
            cfg = await _resolve_embedding_config(db, kb)
            provider = get_provider_for_config(cfg)
            embedder = ProviderEmbedder(provider, model=cfg.embedding_model_name)
            store = get_vector_store()
            retriever = Retriever(embedder, store, NoopReranker())
            hits = await retriever.retrieve(
                question, collection_name(kb.id), top_k=top_k
            )
        except Exception as exc:  # noqa: BLE001 — RAG is best-effort
            logger.warning("RAG retrieval failed for kb %s: %s", kb_id, exc)
            return "", []

        if not hits:
            return "", []

        citations = [self._hit_to_citation(h, i) for i, h in enumerate(hits, start=1)]
        context_block = format_context_block(hits)
        return build_rag_context(context_block), citations

    @staticmethod
    def _hit_to_citation(hit: Any, index: int) -> Citation:
        payload = hit.payload or {}
        text = payload.get("text") or payload.get("content") or ""
        return Citation(
            document_id=payload.get("document_id", ""),
            document_name=payload.get("document_name", "未知来源"),
            chunk_id=payload.get("chunk_id"),
            chunk_index=int(payload.get("chunk_index", index - 1) or 0),
            snippet=text[:300],
            score=float(hit.score or 0.0),
        )


# Module-level singleton — ChatService imports this name directly.
rag_service = RagService()
