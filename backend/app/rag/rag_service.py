"""RagService — the single entry point for knowledge-base retrieval.

ChatService calls ``rag_service.retrieve(db, question, kb_id, top_k)`` and gets back
a context string (ready to splice into the system prompt) plus a list of citations
for the UI. Everything below this (embedder, store, reranker) is an implementation
detail business code never touches.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import KnowledgeBase, ModelConfig
from app.observability import observe_histogram, observe_span
from app.providers.registry import get_provider_for_config
from app.rag.embedder import ProviderEmbedder
from app.rag.fusion import compress_context, rrf_fuse
from app.rag.keyword import KeywordRetriever
from app.rag.prompts import build_rag_context, fit_context, format_context_block
from app.rag.qdrant_store import get_vector_store
from app.rag.reranker import NoopReranker, make_reranker
from app.rag.retriever import Retriever
from app.schemas import Citation

logger = logging.getLogger(__name__)


def collection_name(kb_id: uuid.UUID | str) -> str:
    """Stable Qdrant collection name for a knowledge base.

    Kept here (and imported by document_service) so indexing and retrieval always
    agree on the same collection.
    """
    return "kb_" + str(kb_id).replace("-", "")


def _effective_score(hit: Any) -> float:
    """Best available relevance score for a hit, for the RAG_MIN_SCORE gate.

    Prefers the reranker score (a comparable, model-calibrated relevance) when a
    reranker ran; falls back to the raw ``hit.score`` (cosine similarity in
    pure-vector mode, or a tiny RRF value in hybrid mode).
    """
    rerank = getattr(hit, "rerank_score", None)
    if rerank is not None:
        return float(rerank)
    return float(getattr(hit, "score", 0.0) or 0.0)


# TTL cache for resolved embedding configs. Every retrieval used to re-query
# (and re-decrypt the key of) the KB's embedding ModelConfig per KB per turn;
# model configs change rarely, so a short TTL is a safe trade.
_embedding_cfg_cache: dict[uuid.UUID, tuple[float, ModelConfig | None]] = {}
_embedding_cfg_ttl = 30.0


def _cached_kb_embedding(kb: KnowledgeBase) -> ModelConfig | None:
    """Return the KB's own embedding config from the TTL cache, if fresh.

    NOTE: returns the config only when kb.embedding_model_id is set AND the
    cached lookup succeeded recently; a cache miss returns None so the caller
    falls through to a fresh resolve (which then refreshes the cache).
    """
    if kb.embedding_model_id is None:
        return None
    entry = _embedding_cfg_cache.get(kb.embedding_model_id)
    if entry is None:
        return None
    ts, cfg = entry
    import time as _time

    if _time.monotonic() - ts > _embedding_cfg_ttl:
        return None
    return cfg


def _cache_kb_embedding(kb: KnowledgeBase, cfg: ModelConfig | None) -> None:
    if kb.embedding_model_id is None:
        return
    import time as _time

    _embedding_cfg_cache[kb.embedding_model_id] = (_time.monotonic(), cfg)


async def _resolve_embedding_config(db: AsyncSession, kb: KnowledgeBase) -> ModelConfig:
    """Pick the embedding ModelConfig: the KB's own, else any system/user embedding config."""
    cached = _cached_kb_embedding(kb)
    if cached is not None:
        return cached
    if kb.embedding_model_id is not None:
        cfg = await db.get(ModelConfig, kb.embedding_model_id)
        if cfg is not None:
            _cache_kb_embedding(kb, cfg)
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
        kb_ids: list[uuid.UUID | str],
        top_k: int | None = None,
    ) -> tuple[str, list[Citation]]:
        """Return (rag_context, citations) for the question across one or more KBs.

        Multi-KB: each KB has its own collection (and possibly its own embedding
        model), so we run the hybrid retrieval per-KB, then RRF-fuse the union
        across KBs, rerank, and dedup. Each hit is tagged with its source KB so
        citations name where a chunk came from. Best-effort: any failure
        (no KB, no collection yet, store error) returns ("", []).
        """
        # Observability (Task 11b): one span per retrieval operation. Inert when
        # exporters are off; the question text is NEVER placed in attributes
        # (only the kb count + outcome) so a prompt can't leak into a span.
        import time as _time

        _started = _time.monotonic()
        with observe_span(
            "rag.retrieve",
            kb_count=len(kb_ids) if kb_ids else 0,
            top_k=top_k or 0,
        ):
            ctx, citations = await self._retrieve_impl(db, question, kb_ids, top_k)
        observe_histogram(
            "rag.latency_ms",
            int((_time.monotonic() - _started) * 1000),
            outcome="ok" if ctx else "empty",
        )
        return ctx, citations

    async def _retrieve_impl(
        self,
        db: AsyncSession,
        question: str,
        kb_ids: list[uuid.UUID | str],
        top_k: int | None = None,
    ) -> tuple[str, list[Citation]]:
        if not question or not str(question).strip() or not kb_ids:
            return "", []
        settings = get_settings()
        # A caller-supplied top_k (the retrieval API's explicit parameter) beats
        # every per-KB column; otherwise each KB contributes its own recall
        # window and the request-wide trim is their sum.
        caller_top_k = int(top_k) if top_k else 0
        reranker = make_reranker(settings)
        overfetch = settings.RERANKER_OVERFETCH if not isinstance(reranker, NoopReranker) else 1

        kb_settings: dict[str, _KbRetrieval] = {}
        all_v: list = []
        all_k: list = []
        saw_kb = False
        for kb_id in kb_ids:
            kb = await db.get(KnowledgeBase, uuid.UUID(str(kb_id)))
            if kb is None:
                continue
            saw_kb = True
            ks = _KbRetrieval.from_kb(kb, settings)
            kb_settings[str(kb.id)] = ks
            fetch_k = ks.top_k * max(1, overfetch)
            try:
                cfg = await _resolve_embedding_config(db, kb)
                provider = get_provider_for_config(cfg)
                if not cfg.embedding_model_name:
                    logger.warning(
                        "kb %s has no embedding_model_name; falling back to chat model %r "
                        "— set an embedding model for correct retrieval quality",
                        kb.id, cfg.model_name,
                    )
                embedder = ProviderEmbedder(provider, model=cfg.embedding_model_name)
                store = get_vector_store()
                if settings.RAG_HYBRID:
                    v_hits = await Retriever(embedder, store, None).retrieve(
                        question, collection_name(kb.id), top_k=fetch_k
                    )
                    k_hits = await KeywordRetriever(db).retrieve(
                        question, kb.id, top_k=fetch_k
                    )
                    all_v.extend(_tag_kb(v_hits, kb))
                    all_k.extend(_tag_kb(k_hits, kb))
                else:
                    # Pass reranker=None + overfetch=1: top_k=fetch_k already bakes
                    # in the overfetch multiplier, so letting Retriever re-multiply
                    # would 4x the recall window (and rerank) for nothing. Reranking
                    # happens once, globally, below — mirroring the hybrid branch.
                    v_hits = await Retriever(embedder, store, None).retrieve(
                        question, collection_name(kb.id), top_k=fetch_k, overfetch=1
                    )
                    all_v.extend(_tag_kb(v_hits, kb))
            except Exception as exc:
                logger.warning("RAG retrieval failed for kb %s: %s", kb_id, exc)
                continue

        if not saw_kb:
            return "", []

        # Reranking is one global pass over the fused list, so a KB-level switch
        # can only mean "may I be reranked", not "rerank me alone": it turns the
        # pass off only when every KB in the request opted out.
        if kb_settings and all(v.rerank_enabled is False for v in kb_settings.values()):
            reranker = NoopReranker()
        # Without an explicit caller top_k the request-wide window is the SUM of
        # what each KB asked for: every KB keeps its own recall, and the context
        # token budget below is what actually bounds the prompt.
        final_k = caller_top_k or sum(v.top_k for v in kb_settings.values())

        if settings.RAG_HYBRID:
            fused = rrf_fuse(all_v, all_k, k=settings.RAG_RRF_K)
        else:
            fused = all_v
        if not isinstance(reranker, NoopReranker) and fused:
            fused = await reranker.rerank(question, fused, top_k=final_k * max(1, overfetch))
        hits = fused[: final_k * max(1, overfetch)]
        if settings.RAG_COMPRESS_DEDUP:
            hits = compress_context(hits)
        # Then each KB gets its own recall window back: ``final_k`` is the size of
        # the whole request, so without this a KB configured for top_k=3 could
        # ride a sibling's top_k=12 and dump nine extra chunks into the prompt.
        hits = _cap_per_kb(hits, kb_settings)[:final_k]
        if not hits:
            return "", []

        # Relevance gate: drop chunks below their own KB's threshold. The most
        # comparable score is the reranker score when a reranker ran; otherwise
        # the raw (possibly RRF-fused) score. With the default 0.0 nothing is
        # filtered; when tuned, a turn with NO chunk clearing the bar returns
        # empty context + empty citations so low-relevance snippets never
        # pollute the answer. Per-KB rather than global because a small
        # technical KB and a sprawling legal one do not calibrate alike.
        retrieved_count = len(hits)
        top_score = max((_effective_score(h) for h in hits), default=0.0)
        min_score = float(getattr(settings, "RAG_MIN_SCORE", 0.0))
        hits = [h for h in hits if _effective_score(h) >= _threshold_of(h, kb_settings, min_score)]
        accepted_count = len(hits)
        logger.info(
            "rag_retrieval kb_ids=%s retrieved_count=%d accepted_count=%d "
            "top_score=%.4f min_score=%.4f",
            [str(k) for k in kb_ids], retrieved_count, accepted_count,
            top_score, min_score,
        )
        if not hits:
            return "", []

        # Token budget last: citations must be built from the SAME list the
        # context block was rendered from, or the UI would credit sources the
        # model never saw. Chunk *count* was the only previous bound on prompt
        # size, so a long-document KB with top_k=12 paid for ~18k tokens a turn.
        hits = fit_context(hits, getattr(settings, "RAG_CONTEXT_TOKENS", 0) or None)
        if not hits:
            return "", []
        citations = [self._hit_to_citation(h, i) for i, h in enumerate(hits, start=1)]
        context_block = format_context_block(hits)
        return build_rag_context(context_block), citations

    @staticmethod
    def _hit_to_citation(hit: Any, index: int) -> Citation:
        payload = hit.payload or {}
        text = payload.get("text") or payload.get("content") or ""
        rerank = getattr(hit, "rerank_score", None)
        return Citation(
            document_id=payload.get("document_id") or None,
            document_name=payload.get("document_name", "未知来源"),
            chunk_id=payload.get("chunk_id"),
            chunk_index=int(payload.get("chunk_index", index - 1) or 0),
            snippet=text[:300],
            score=float(hit.score or 0.0),
            source_type="document",
            page_number=payload.get("page"),
            rerank_score=float(rerank) if rerank is not None else None,
            metadata={
                "collection": payload.get("collection"),
                "kb_id": payload.get("kb_id"),
                "kb_name": payload.get("kb_name"),
                "heading": payload.get("heading"),
                "retriever": payload.get("retriever"),
            },
        )


def _tag_kb(hits: list, kb: KnowledgeBase) -> list:
    """Stamp each hit's payload with its source KB id/name (for citations)."""
    for h in hits:
        p = dict(h.payload or {})
        p.setdefault("kb_id", str(kb.id))
        p.setdefault("kb_name", kb.name)
        # Vectors written before this column existed carry no collection in their
        # payload, and keyword hits never did, so stamp it here too — otherwise
        # ``citation.metadata["collection"]`` stays None for every lexical hit.
        p.setdefault("collection", collection_name(kb.id))
        h.payload = p
    return hits


@dataclass(frozen=True)
class _KbRetrieval:
    """One KB's effective retrieval parameters for this request.

    Columns are nullable on purpose (NULL = inherit the global default), so this
    is where the fallback happens and nowhere else.
    """

    top_k: int
    score_threshold: float
    rerank_enabled: bool | None

    @classmethod
    def from_kb(cls, kb: KnowledgeBase, settings: Any) -> _KbRetrieval:
        """Resolve one KB's columns against the platform defaults.

        Every column left NULL — which is every KB created before this feature
        existed — inherits the operator's global setting, so turning the columns
        on cannot silently re-tune retrieval for existing knowledge bases.
        """
        return cls(
            top_k=_positive_int(kb.top_k, int(settings.RAG_TOP_K)),
            score_threshold=_nonneg_float(
                kb.score_threshold, float(getattr(settings, "RAG_MIN_SCORE", 0.0) or 0.0)
            ),
            rerank_enabled=kb.rerank_enabled,
        )


def _threshold_of(hit: Any, kb_settings: dict[str, _KbRetrieval], default: float) -> float:
    """The relevance bar this hit's own KB set, or ``default`` when untagged."""
    payload = getattr(hit, "payload", None) or {}
    ks = kb_settings.get(str(payload.get("kb_id") or ""))
    return ks.score_threshold if ks is not None else default


def _cap_per_kb(hits: list, kb_settings: dict[str, _KbRetrieval]) -> list:
    """Keep at most each KB's own ``top_k`` hits, in the fused list's order."""
    if not kb_settings:
        return hits
    seen: dict[str, int] = {}
    out: list = []
    for hit in hits:
        payload = getattr(hit, "payload", None) or {}
        key = str(payload.get("kb_id") or "")
        ks = kb_settings.get(key)
        if ks is not None:
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > ks.top_k:
                continue
        out.append(hit)
    return out


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _nonneg_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0.0 else default


# Module-level singleton — ChatService imports this name directly.
rag_service = RagService()
