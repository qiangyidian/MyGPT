"""RagService — the single entry point for knowledge-base retrieval.

ChatService calls ``rag_service.retrieve(db, question, kb_ids, top_k)`` and gets back
a context string (ready to splice into the system prompt) plus a list of citations
for the UI. Everything below this (embedder, store, reranker) is an implementation
detail business code never touches.

**两个入口，一份实现。** 设计文档要求各阶段产出强类型契约（RAG-06 QueryPlan /
RAG-07 CandidateSet / RAG-09 ContextPack），而这三个对象正是生成后校验（RAG-13
「引用 ID 必须属于本次请求」）唯一能依靠的判定表。但 ``retrieve()`` 已经被三个调用
点依赖，且其中 durable 那条路径本身就带着租约与并发复杂度，不该被本切片顺带拉进来
改造。所以：

  * ``retrieve_with_trace()`` —— 新入口，返回 ``RetrievalOutcome``（含三份契约、
    引用列表与 ``[source N] → ContextItem`` 判定表）；
  * ``retrieve()`` —— 保持原签名与原始行为的薄包装，内部就是调上面那个然后丢掉
    trace。**没有第二份实现**，两条路不可能各自漂移。

一个必须守住的不变量：**引用与上下文必须来自同一份列表**。``[source 3]`` 的序号是
``format_context_block`` 按列表顺序编的，引用面板再从同一个列表生成——一旦两边各自
过滤，用户点开就会看到模型从未见过的那段文字。
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Document, DocumentChunk, KnowledgeBase, ModelConfig
from app.observability import observe_histogram, observe_span
from app.providers.registry import get_provider_for_config
from app.rag.embedder import ProviderEmbedder
from app.rag.base import SearchHit
from app.rag.budget import compute_context_budget
from app.rag.contracts import (
    Candidate,
    CandidateSet,
    ContextItem,
    ContextPack,
    DroppedItem,
    QueryPlan,
    RetrievalSnapshot,
    RetrievalTrace,
    new_trace_id,
)
from app.rag.fusion import compress_context, rrf_fuse
from app.rag.keyword import KeywordRetriever
from app.rag.prompts import build_rag_context, fit_context, format_context_block
from app.rag.qdrant_store import get_vector_store
from app.rag.query_plan import build_query_plan
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


def filter_hash(
    *,
    kb_ids: list[str],
    filters: dict[str, Any],
    user_id: uuid.UUID | str | None,
) -> str:
    """授权范围 + 过滤条件的稳定哈希（文档 RAG-04：缓存键必须包含授权范围）。

    今天它是检索诊断字段（两路召回是否真的跑在同一范围上）；一旦引入缓存，它就是
    缓存键的授权维度——没有它，一个用户拿到的上下文可能被另一个用户复用。
    """
    import hashlib
    import json

    payload = json.dumps(
        {
            "kb": sorted(str(k) for k in kb_ids),
            "filters": {k: sorted(map(str, v)) if isinstance(v, (list, tuple, set)) else str(v)
                        for k, v in sorted(filters.items())},
            "principal": str(user_id or ""),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class RetrievalOutcome:
    """``retrieve_with_trace`` 的返回值。"""

    trace: RetrievalTrace
    citations: list[Citation]

    @property
    def context_text(self) -> str:
        return self.trace.snapshot.context_text

    @property
    def source_index(self) -> dict[int, ContextItem]:
        return self.trace.source_index


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
    """Orchestrates plan -> embed -> search -> fuse -> (rerank) -> ContextPack."""

    # ----------------------------------------------------------------- #
    # 公开入口 1：保持原签名（三个既有调用点与既有测试都靠它）
    # ----------------------------------------------------------------- #
    async def retrieve(
        self,
        db: AsyncSession,
        question: str,
        kb_ids: list[uuid.UUID | str],
        top_k: int | None = None,
        document_ids: list[uuid.UUID | str] | None = None,
    ) -> tuple[str, list[Citation]]:
        """Return (rag_context, citations) for the question across one or more KBs.

        Multi-KB: each KB has its own collection (and possibly its own embedding
        model), so we run the hybrid retrieval per-KB, then RRF-fuse the union
        across KBs, rerank, and dedup. Each hit is tagged with its source KB so
        citations name where a chunk came from. Best-effort: any failure
        (no KB, no collection yet, store error) returns ("", []).

        ``document_ids`` narrows the turn to specific documents (an ``@文件``
        reference): the filter applies to each KB's own hits *before* fusion, so
        the cross-KB merge, the per-KB recall cap, the relevance gate and the
        token budget all keep working unchanged. ``top_k`` stays the request-wide
        override it has always been: unset, every KB keeps its own window.
        """
        outcome = await self.retrieve_with_trace(
            db, question, kb_ids, top_k=top_k, document_ids=document_ids
        )
        return outcome.context_text, outcome.citations

    # ----------------------------------------------------------------- #
    # 公开入口 2：带完整阶段契约（生成后校验依赖它）
    # ----------------------------------------------------------------- #
    async def retrieve_with_trace(
        self,
        db: AsyncSession,
        question: str,
        kb_ids: list[uuid.UUID | str],
        top_k: int | None = None,
        document_ids: list[uuid.UUID | str] | None = None,
        *,
        trace_id: str | None = None,
        user_id: uuid.UUID | str | None = None,
        context_budget_tokens: int | None = None,
    ) -> RetrievalOutcome:
        """与 ``retrieve`` 同一条实现，但把 QueryPlan / CandidateSet / ContextPack 交出来。

        ``context_budget_tokens`` 由调用方按**生成模型**的窗口算好传进来（见
        ``rag.budget``）：预算取决于生成模型的上下文窗口，而这里解析的是 KB 的
        **嵌入**模型 —— 两者不是同一个模型，在本层算预算只能猜。传 ``None`` 时回退
        到 ``RAG_CONTEXT_TOKENS`` 的历史行为。
        """
        started = time.monotonic()
        trace_id = trace_id or new_trace_id()
        with observe_span(
            "rag.retrieve",
            kb_count=len(kb_ids) if kb_ids else 0,
            top_k=top_k or 0,
        ):
            outcome = await self._retrieve_impl(
                db,
                question,
                kb_ids,
                top_k,
                document_ids,
                trace_id=trace_id,
                user_id=user_id,
                context_budget_tokens=context_budget_tokens,
            )
        elapsed = int((time.monotonic() - started) * 1000)
        observe_histogram(
            "rag.latency_ms",
            elapsed,
            outcome="ok" if outcome.context_text else "empty",
        )
        # 检索总耗时回填进快照：TraceSummary 要能回答「这轮慢在哪一段」。
        outcome.trace.snapshot.latency_ms = elapsed
        return outcome

    # ----------------------------------------------------------------- #
    # 实现
    # ----------------------------------------------------------------- #
    async def _retrieve_impl(
        self,
        db: AsyncSession,
        question: str,
        kb_ids: list[uuid.UUID | str],
        top_k: int | None = None,
        document_ids: list[uuid.UUID | str] | None = None,
        *,
        trace_id: str | None = None,
        user_id: uuid.UUID | str | None = None,
        context_budget_tokens: int | None = None,
    ) -> RetrievalOutcome:
        settings = get_settings()
        trace_id = trace_id or new_trace_id()
        # ---- RAG-06：先把问题变成一个可解释的计划 -------------------------
        plan = build_query_plan(
            question or "",
            document_ids=[str(d) for d in (document_ids or [])] or None,
            trace_id=trace_id,
        )
        empty = self._empty_outcome(plan, trace_id)
        if not question or not str(question).strip() or not kb_ids:
            return empty
        from app.agents.planning import is_document_inventory_question

        if is_document_inventory_question(question):
            return await self._document_inventory(
                db, kb_ids, document_ids, plan=plan, trace_id=trace_id
            )

        # 计划里真正能下推的过滤条件：今天只有 document_id（见 query_plan 的白名单）。
        plan_filters = plan.filter_map()
        doc_scope = {str(d) for d in (plan_filters.get("document_id") or [])} or None
        fhash = filter_hash(kb_ids=[str(k) for k in kb_ids], filters=plan_filters, user_id=user_id)

        # A caller-supplied top_k (the retrieval API's explicit parameter) beats
        # every per-KB column; otherwise each KB contributes its own recall
        # window and the request-wide trim is their sum.
        caller_top_k = int(top_k) if top_k else 0
        reranker = make_reranker(settings)
        overfetch = settings.RERANKER_OVERFETCH if not isinstance(reranker, NoopReranker) else 1

        # 纯编号/错误码查询必须有关键词召回通道（文档 RAG-06 安全与回退）：语义向量
        # 找 E1001 本来就不可靠，hybrid 关掉时更不能只剩 dense。
        hybrid = bool(settings.RAG_HYBRID) or bool(plan.requires_exact_match)
        fallbacks: list[str] = []
        if plan.requires_exact_match and not settings.RAG_HYBRID:
            fallbacks.append("keyword_forced_by_exact_match")

        kb_settings: dict[str, _KbRetrieval] = {}
        all_v: list[SearchHit] = []
        all_k: list[SearchHit] = []
        candidates: list[Candidate] = []
        retrievers: list[str] = []
        saw_kb = False
        for kb_id in kb_ids:
            kb = await db.get(KnowledgeBase, uuid.UUID(str(kb_id)))
            if kb is None:
                continue
            saw_kb = True
            ks = _KbRetrieval.from_kb(kb, settings)
            kb_settings[str(kb.id)] = ks
            # Candidate recall and answer context have different budgets. In
            # hybrid mode, each retriever gets a wider window for fusion; the
            # per-KB top_k remains the final context limit below.
            retrieval_k = ks.top_k
            if hybrid:
                retrieval_k = max(
                    retrieval_k,
                    _positive_int(
                        getattr(settings, "RAG_RETRIEVAL_CANDIDATES", 30), 30
                    ),
                )
            fetch_k = retrieval_k if hybrid else retrieval_k * max(1, overfetch)
            document_filter = {"document_id": sorted(doc_scope)} if doc_scope else None
            vector_filters = {"filters": document_filter} if document_filter else {}
            keyword_filters = {"document_ids": sorted(doc_scope)} if doc_scope else {}
            if hybrid:
                # Keep lexical recall independent: an embedding/provider outage
                # should still return useful exact-term matches.
                try:
                    with observe_span(
                        "rag.retrieve.keyword",
                        candidate_limit=fetch_k,
                        scoped=bool(doc_scope),
                    ) as span:
                        k_hits = await KeywordRetriever(db).retrieve(
                            question, kb.id, top_k=fetch_k, **keyword_filters
                        )
                        span.set_attribute("result_count", len(k_hits))
                    all_k.extend(_tag_kb(k_hits, kb))
                    if k_hits and "keyword" not in retrievers:
                        retrievers.append("keyword")
                    candidates.extend(
                        _candidates_of(k_hits, "keyword", fhash, score_type="bm25ish")
                    )
                except Exception as exc:
                    logger.warning(
                        "RAG keyword retrieval failed for kb %s: %s", kb.id, exc
                    )
                    fallbacks.append("keyword_failed")

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
                with observe_span(
                    "rag.retrieve.dense",
                    candidate_limit=fetch_k,
                    scoped=bool(doc_scope),
                ) as span:
                    # Pass reranker=None + overfetch=1: ``fetch_k`` already bakes in
                    # the configured overfetch multiplier, so letting Retriever
                    # re-multiply would 4x the recall window for nothing. Reranking
                    # happens once, globally, below.
                    v_hits = await Retriever(embedder, store, None).retrieve(
                        question,
                        collection_name(kb.id),
                        top_k=fetch_k,
                        overfetch=1,
                        **vector_filters,
                    )
                    span.set_attribute("result_count", len(v_hits))
                all_v.extend(_tag_kb(v_hits, kb))
                if v_hits and "dense" not in retrievers:
                    retrievers.append("dense")
                candidates.extend(_candidates_of(v_hits, "dense", fhash, score_type="cosine"))
            except Exception as exc:
                logger.warning("RAG dense retrieval failed for kb %s: %s", kb_id, exc)
                fallbacks.append("dense_failed")

        if not saw_kb:
            return empty

        # Reranking is one global pass over the fused list, so a KB-level switch
        # can only mean "may I be reranked", not "rerank me alone": it turns the
        # pass off only when every KB in the request opted out.
        if kb_settings and all(v.rerank_enabled is False for v in kb_settings.values()):
            reranker = NoopReranker()
        # Without an explicit caller top_k the request-wide window is the SUM of
        # what each KB asked for: every KB keeps its own recall, and the context
        # token budget below is what actually bounds the prompt.
        final_k = caller_top_k or sum(v.top_k for v in kb_settings.values())

        if hybrid:
            with observe_span(
                "rag.retrieve.fusion",
                dense_count=len(all_v),
                keyword_count=len(all_k),
                rrf_k=settings.RAG_RRF_K,
            ) as span:
                fused = rrf_fuse(all_v, all_k, k=settings.RAG_RRF_K)
                span.set_attribute("result_count", len(fused))
        else:
            fused = all_v
        fused_limit = _positive_int(
            getattr(settings, "RAG_MAX_FUSED_CANDIDATES", 60), 60
        )
        fused = fused[:fused_limit]
        if not isinstance(reranker, NoopReranker) and fused:
            with observe_span(
                "rag.retrieve.rerank",
                candidate_count=len(fused),
                output_limit=len(fused),
            ) as span:
                fused = await reranker.rerank(question, fused, top_k=len(fused))
                span.set_attribute("result_count", len(fused))
        hits = fused
        dropped: list[DroppedItem] = []
        if settings.RAG_COMPRESS_DEDUP:
            before = {h.id for h in hits}
            hits = compress_context(hits)
            after = {h.id for h in hits}
            dropped.extend(
                DroppedItem(chunk_id=cid, reason="duplicate_or_budget")
                for cid in before - after
            )
        # Then each KB gets its own recall window back: ``final_k`` is the size of
        # the whole request, so without this a KB configured for top_k=3 could
        # ride a sibling's top_k=12 and dump nine extra chunks into the prompt.
        if not caller_top_k:
            before = {h.id for h in hits}
            hits = _cap_per_kb(hits, kb_settings)
            dropped.extend(
                DroppedItem(chunk_id=cid, reason="per_kb_cap")
                for cid in before - {h.id for h in hits}
            )
        before = {h.id for h in hits}
        hits = _cap_per_document(
            hits,
            _positive_int(
                getattr(settings, "RAG_MAX_CHUNKS_PER_DOCUMENT", 3), 3
            ),
        )
        dropped.extend(
            DroppedItem(chunk_id=cid, reason="per_document_cap")
            for cid in before - {h.id for h in hits}
        )
        hits = hits[:final_k]
        if not hits:
            return self._empty_outcome(
                plan, trace_id, retrievers=retrievers, candidates=candidates,
                fallbacks=fallbacks, insufficient=True,
            )

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
        kept: list[SearchHit] = []
        for h in hits:
            if _effective_score(h) >= _threshold_of(h, kb_settings, min_score):
                kept.append(h)
            else:
                dropped.append(DroppedItem(chunk_id=h.id, reason="below_threshold"))
        hits = kept
        accepted_count = len(hits)
        logger.info(
            "rag_retrieval kb_ids=%s retrieved_count=%d accepted_count=%d "
            "top_score=%.4f min_score=%.4f filters_applied=%d filters_rejected=%d",
            [str(k) for k in kb_ids], retrieved_count, accepted_count,
            top_score, min_score, len(plan.filters), len(plan.rejected_filters),
        )
        if not hits:
            return self._empty_outcome(
                plan, trace_id, retrievers=retrievers, candidates=candidates,
                fallbacks=fallbacks, insufficient=True,
            )

        # Token budget last: citations must be built from the SAME list the
        # context block was rendered from, or the UI would credit sources the
        # model never saw. Chunk *count* was the only previous bound on prompt
        # size, so a long-document KB with top_k=12 paid for ~18k tokens a turn.
        budget = compute_context_budget(
            context_window=None,
            absolute_fallback=(
                int(context_budget_tokens)
                if context_budget_tokens is not None
                else int(getattr(settings, "RAG_CONTEXT_TOKENS", 0) or 0)
            ),
        )
        with observe_span(
            "rag.retrieve.context_pack",
            candidate_count=len(hits),
            token_budget=budget.budget,
        ) as span:
            before = {h.id for h in hits}
            hits = fit_context(hits, budget.budget or None)
            dropped.extend(
                DroppedItem(chunk_id=cid, reason="duplicate_or_budget")
                for cid in before - {h.id for h in hits}
            )
            span.set_attribute("result_count", len(hits))
        if not hits:
            return self._empty_outcome(
                plan, trace_id, retrievers=retrievers, candidates=candidates,
                fallbacks=fallbacks, insufficient=True,
            )

        # ---- RAG-09：ContextPack + [source N] 判定表 -----------------------
        shas = await self._content_hashes(db, hits)
        items = [_hit_to_context_item(h, i, shas) for i, h in enumerate(hits, start=1)]
        source_index = dict(enumerate(items, start=1))
        # 引用从**同一份 hits** 生成，而不是从 ContextItem 反推：引用要携带 KB 名、
        # collection 与各召回通道的名次（UI 与调试面板都读它），那些字段只活在 payload
        # 上。从 ContextItem 反推会把它们丢掉，也会让 citation 与 context block 成为
        # 两份各自演化的列表 —— 那正是「用户点开看到模型没见过的文字」的成因。
        context_block = format_context_block(hits)
        pack = ContextPack(
            request_id=plan.request_id,
            trace_id=trace_id,
            budget_tokens=budget.budget,
            used_tokens=sum(_tokens_of(h) for h in hits),
            items=items,
            dropped=dropped,
            insufficient_evidence=False,
        )
        candidate_set = CandidateSet(
            request_id=plan.request_id,
            trace_id=trace_id,
            retrievers=retrievers,
            candidates=candidates[: _positive_int(
                getattr(settings, "RAG_TRACE_MAX_CANDIDATES", 120), 120
            )],
            fusion=(
                {"method": "rrf", "params": {"k": settings.RAG_RRF_K}}
                if hybrid
                else {"method": "none"}
            ),
            candidate_count=len(all_v) + len(all_k),
            fallbacks=fallbacks,
        )
        snapshot = RetrievalSnapshot(
            trace_id=trace_id,
            query_plan=plan,
            candidate_set=candidate_set,
            context_pack=pack,
            context_text=build_rag_context(context_block),
        )
        citations = [_hit_to_citation(h, i) for i, h in enumerate(hits, start=1)]
        return RetrievalOutcome(
            trace=RetrievalTrace(snapshot=snapshot, source_index=source_index),
            citations=citations,
        )

    # ---- 辅助 -------------------------------------------------------------- #
    @staticmethod
    async def _content_hashes(db: AsyncSession, hits: list[SearchHit]) -> dict[str, str]:
        """批量取这批分片的内容哈希。

        取它是为了让生成后校验能判断「这份引用指向的版本还在不在」——文档在检索与
        用户点开引用之间可能被重新索引过。批量查一次（≤ 上下文分片数，走主键），
        而不是每个分片查一次。Qdrant payload 里没有这个字段，且既有向量不会补写，
        所以只能从库里读。
        """
        ids: list[uuid.UUID] = []
        for hit in hits:
            try:
                ids.append(uuid.UUID(str(hit.id)))
            except (ValueError, AttributeError, TypeError):
                continue
        if not ids:
            return {}
        rows = (
            await db.execute(
                select(DocumentChunk.id, DocumentChunk.content_sha256).where(
                    DocumentChunk.id.in_(ids)
                )
            )
        ).all()
        return {str(cid): sha for cid, sha in rows if sha}

    def _empty_outcome(
        self,
        plan: QueryPlan,
        trace_id: str,
        *,
        retrievers: list[str] | None = None,
        candidates: list[Candidate] | None = None,
        fallbacks: list[str] | None = None,
        insufficient: bool = False,
    ) -> RetrievalOutcome:
        return RetrievalOutcome(
            trace=RetrievalTrace(
                snapshot=RetrievalSnapshot(
                    trace_id=trace_id,
                    query_plan=plan,
                    candidate_set=CandidateSet(
                        request_id=plan.request_id,
                        trace_id=trace_id,
                        retrievers=list(retrievers or []),
                        candidates=list(candidates or [])[
                            : _positive_int(
                                getattr(get_settings(), "RAG_TRACE_MAX_CANDIDATES", 120), 120
                            )
                        ],
                        candidate_count=len(candidates or []),
                        fallbacks=list(fallbacks or []),
                    ),
                    context_pack=ContextPack(
                        request_id=plan.request_id,
                        trace_id=trace_id,
                        insufficient_evidence=insufficient,
                    ),
                ),
                source_index={},
            ),
            citations=[],
        )

    async def _document_inventory(
        self,
        db: AsyncSession,
        kb_ids: list[uuid.UUID | str],
        document_ids: list[uuid.UUID | str] | None,
        *,
        plan: QueryPlan | None = None,
        trace_id: str | None = None,
    ) -> RetrievalOutcome:
        # Ownership of these KB IDs is checked by the calling chat/RAG API.
        # Inventory is metadata, not a similarity search: unindexed files must
        # still appear, and no embedding endpoint or vector collection is needed.
        plan = plan or build_query_plan("", trace_id=trace_id)
        trace_id = trace_id or plan.trace_id
        scope = [Document.knowledge_base_id.in_([uuid.UUID(str(k)) for k in kb_ids])]
        if document_ids:
            scope.append(Document.id.in_([uuid.UUID(str(d)) for d in document_ids]))
        total = await db.scalar(select(func.count(Document.id)).where(*scope)) or 0
        rows = (await db.execute(
            select(Document, KnowledgeBase.name, DocumentChunk.id,
                   func.substr(DocumentChunk.content, 1, 400))
            .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
            .outerjoin(DocumentChunk, (DocumentChunk.document_id == Document.id)
                       & (DocumentChunk.chunk_index == 0))
            .where(*scope)
            .order_by(Document.created_at, Document.id)
            .limit(50)
        )).all()
        hits = [SearchHit(
            id=str(chunk_id) if chunk_id else "",
            score=1.0,
            payload={
                "document_id": str(doc.id), "document_name": doc.filename,
                "kb_id": str(doc.knowledge_base_id), "kb_name": kb_name,
                "chunk_id": str(chunk_id) if chunk_id else None, "chunk_index": 0,
                "retriever": "document_inventory",
                "text": (f"文件名：{doc.filename}\n知识库：{kb_name}\n"
                         f"类型：{doc.file_type}；索引状态：{doc.status}\n"
                         f"开头预览：{preview or '暂无已解析正文'}"),
            },
        ) for doc, kb_name, chunk_id, preview in rows]
        hits = fit_context(hits, get_settings().RAG_CONTEXT_TOKENS or None)
        header = f"所选知识库共 {total} 份文档，本次展示 {len(hits)} 份。"
        if len(hits) < total:
            header += "这里只是部分清单，不要声称已展示全部文件。"
        items = [_hit_to_context_item(h, i, {}) for i, h in enumerate(hits, start=1)]
        source_index = dict(enumerate(items, start=1))
        pack = ContextPack(
            request_id=plan.request_id,
            trace_id=trace_id,
            budget_tokens=int(getattr(get_settings(), "RAG_CONTEXT_TOKENS", 0) or 0),
            items=items,
        )
        return RetrievalOutcome(
            trace=RetrievalTrace(
                snapshot=RetrievalSnapshot(
                    trace_id=trace_id,
                    query_plan=plan,
                    candidate_set=CandidateSet(
                        request_id=plan.request_id,
                        trace_id=trace_id,
                        retrievers=["document_inventory"],
                        candidate_count=len(items),
                    ),
                    context_pack=pack,
                    context_text=header + "\n" + format_context_block(hits),
                ),
                source_index=source_index,
            ),
            citations=[_hit_to_citation(hit, i) for i, hit in enumerate(hits, start=1)],
        )


# --------------------------------------------------------------------------- #
# 命中 → 契约对象
# --------------------------------------------------------------------------- #
def _tag_kb(hits: list, kb: KnowledgeBase) -> list:
    """Stamp each hit's payload with its source KB id/name (for citations)."""
    for h in hits:
        p = dict(h.payload or {})
        p.setdefault("kb_id", str(kb.id))
        p.setdefault("kb_name", kb.name)
        p.setdefault("retriever", "dense")
        # Vectors written before this column existed carry no collection in their
        # payload, and keyword hits never did, so stamp it here too — otherwise
        # ``citation.metadata["collection"]`` stays None for every lexical hit.
        p.setdefault("collection", collection_name(kb.id))
        h.payload = p
    return hits


def _candidates_of(
    hits: list[SearchHit], retriever: str, fhash: str, *, score_type: str
) -> list[Candidate]:
    """把一路召回的命中记成候选（带它在这一路里的名次与原始分）。

    保留原始名次与原始分而不是只留融合分，是文档 RAG-07「排序要求」的硬要求：
    只有融合分时无法判断某一路是否在做负贡献，也就无法决定该不该关掉它。
    """
    out: list[Candidate] = []
    for rank, hit in enumerate(hits, start=1):
        out.append(
            Candidate(
                chunk_id=str(hit.id),
                retriever=retriever,
                rank=rank,
                raw_score=float(hit.score or 0.0),
                score_type=score_type,
                filter_hash=fhash,
            )
        )
    return out


def _source_spans(payload: dict[str, Any]) -> list[dict[str, Any]]:
    span: dict[str, Any] = {}
    for key in ("page", "char_start", "char_end"):
        value = payload.get(key)
        if value is not None:
            span[key] = value
    if payload.get("heading"):
        span["heading"] = payload["heading"]
    return [span] if span else []


def _tokens_of(hit: Any) -> int:
    from app.rag.prompts import _token_counter

    payload = getattr(hit, "payload", None) or {}
    text = payload.get("text") or payload.get("content") or ""
    try:
        return int(_token_counter()(text))
    except Exception:  # pragma: no cover - 分词器缺失时的粗估
        return max(1, len(text) // 4)


def _hit_to_context_item(
    hit: Any, index: int, shas: dict[str, str]
) -> ContextItem:
    """一个命中 → ContextItem。``context_id`` 是本次请求内的引用锚点。"""
    payload = getattr(hit, "payload", None) or {}
    chunk_id = str(payload.get("chunk_id") or hit.id or "")
    rerank = getattr(hit, "rerank_score", None)
    return ContextItem(
        context_id=f"ctx-{index}",
        chunk_id=chunk_id,
        text=payload.get("text") or payload.get("content") or "",
        document_id=payload.get("document_id") or None,
        document_name=payload.get("document_name") or payload.get("source") or "未知来源",
        source_spans=_source_spans(payload),
        selection_reason="high_rerank_score" if rerank is not None else "fused_rank",
        score=_effective_score(hit),
        content_sha256=shas.get(chunk_id),
    )


def _hit_to_citation(hit: Any, index: int) -> Citation:
    """一个命中 → 对外的 ``Citation``。

    ``validation`` 留**空**（三项全 ``None``）：它由生成后的门禁
    （``rag.postprocess``）回填。初值必须是「未校验」而不是「通过」—— 默认通过等于
    把「还没查」伪装成「查过了」，而这正是本切片要消灭的那类谎。
    """
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
            "retrievers": payload.get("retrievers", []),
            "retrieval_channels": payload.get("retrieval_channels", {}),
            # ``[source N]`` 的序号 = 它在 ContextPack 里的位置，也就是
            # ``ctx-N``。生成后校验正是靠它把「模型引用的序号」映射回
            # 「本次请求真的给过它的那段证据」。
            "context_id": f"ctx-{index}",
            "source_span": {
                key: payload[key]
                for key in ("page", "char_start", "char_end")
                if payload.get(key) is not None
            },
        },
    )


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

        ``RAG_MAX_CHUNKS_PER_KB`` 是文档 RAG-08/RAG-09 的「最多 8 个 Chunk」上限
        （``0`` = 不设平台上限，由每库自己的 ``top_k`` 决定）。它是**平台上限**而不是
        默认值：``RAG_TOP_K=5`` 已经落在文档建议的 5–8 区间内，这一列只用来拦住
        「某库被配成 12」这种越界。
        """
        resolved_top_k = _positive_int(kb.top_k, int(settings.RAG_TOP_K))
        cap = int(getattr(settings, "RAG_MAX_CHUNKS_PER_KB", 0) or 0)
        return cls(
            top_k=min(resolved_top_k, cap) if cap > 0 else resolved_top_k,
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


def _cap_per_document(hits: list, limit: int) -> list:
    """Bound one document's share of context while preserving ranked order."""
    if limit <= 0:
        return hits
    counts: dict[str, int] = {}
    out: list = []
    for hit in hits:
        payload = getattr(hit, "payload", None) or {}
        # Unattributed hits should not all collapse into one bucket.
        document_id = str(payload.get("document_id") or hit.id)
        count = counts.get(document_id, 0)
        if count >= limit:
            continue
        counts[document_id] = count + 1
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
