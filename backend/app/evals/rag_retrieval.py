"""检索质量评测的执行层：golden set + 一个检索器 → 一组可门禁的指标。

离线那一遍（:func:`run_rag_retrieval_eval`）不接 Qdrant、不接模型，用一个 BM25
基线检索器把整条评测链路跑通。它真正的价值不是「基线分数够不够高」，而是两件事：

  * 指标与 golden set 本身被锁住——标注被改坏、指标算错，门禁立刻红；
  * 分词是被复用的生产代码（``app.rag.keyword``）。中文 bigram 一旦退化，基线
    召回会掉到接近 0，这正是历史上真实发生过一次的静默故障。

在线那一遍（:func:`run_live_rag_retrieval_eval`）才测真正的混合召回，需要一套装好
了这批文档的知识库：没配 ``EVAL_RAG_KB_IDS`` 就 SKIP，配了但配错则 FAIL。
"""
from __future__ import annotations

import math
import os
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.evals.rag_golden import GoldenCorpus, load_golden_corpus
from app.evals.rag_metrics import (
    QueryMetrics,
    citation_precision,
    evaluate_query,
    summarize,
    ungrounded_citations,
)
from app.rag.keyword import _tokenize

__all__ = [
    "Retrieved",
    "bm25_retriever",
    "evaluate_corpus",
    "run_rag_retrieval_eval",
    "run_live_rag_retrieval_eval",
]

_CONTRACT = "rag_retrieval_quality"
_LIVE_CONTRACT = "rag_retrieval_live"
_LIVE_ENV = "EVAL_RAG_KB_IDS"


@dataclass(frozen=True)
class Retrieved:
    """一条命中：``key`` 是被打分的那个标识（切片 id 或文档名），``text`` 是正文。"""

    key: str
    text: str = ""
    score: float = 0.0


Retriever = Callable[[str], Sequence[Retrieved]]


@dataclass(frozen=True)
class Thresholds:
    """门禁底线，按 BM25 基线实测值留了余量（recall 0.93 / nDCG 0.91 / MRR 0.91）。

    ``precision@k`` 与引用准确率**只报不卡**（默认 0）：二值标注下每条查询通常只
    有一个正确切片，precision@k 的上界就是 ``|relevant|/k``——20 块语料、k=5 时
    天花板只有 0.2。拿它当门禁卡的是「语料够不够大」，不是「检索好不好」；真要
    卡的是引用溯源（snippet 必须来自它声称的那一块）和排序质量。
    """

    recall: float = 0.85
    ndcg: float = 0.80
    precision: float = 0.0
    mrr: float = 0.80
    map: float = 0.80
    citation: float = 0.0
    #: 引用溯源必须完全干净：snippet 不属于它声称的那一块，用户点开就是打脸。
    grounded: float = 1.0


@dataclass(frozen=True)
class EvalResult:
    summary: dict[str, Any]
    metrics: tuple[QueryMetrics, ...] = field(default_factory=tuple)
    ungrounded: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "ungrounded": list(self.ungrounded),
            "failures": list(self.failures),
            "ok": self.ok,
        }


# --------------------------------------------------------------------------- #
# Offline baseline retriever: BM25 over the golden corpus.
# --------------------------------------------------------------------------- #
def _terms(text: str) -> list[str]:
    latin, grams = _tokenize(text)
    return latin + grams


def bm25_retriever(
    corpus: GoldenCorpus,
    *,
    k1: float = 1.2,
    b: float = 0.75,
    top_k: int = 10,
) -> Retriever:
    """A dependency-free BM25 over one corpus — enough to rank 20 hand-written chunks.

    The document frequency table is built once here (a pure function of the
    corpus), so the returned retriever is deterministic and cheap to call.
    """
    docs = [(_terms(c["text"]), c) for c in corpus.chunks]
    doc_count = len(docs) or 1
    avg_len = sum(len(tokens) for tokens, _ in docs) / doc_count
    df: dict[str, int] = {}
    for tokens, _ in docs:
        for token in set(tokens):
            df[token] = df.get(token, 0) + 1
    idf = {
        token: math.log(1 + (doc_count - n + 0.5) / (n + 0.5)) for token, n in df.items()
    }
    # Term counts are a property of the corpus, not of the query: count them once.
    scored_docs: list[tuple[dict[str, int], float, dict[str, Any]]] = [
        (
            {token: tokens.count(token) for token in set(tokens)},
            float(len(tokens)),
            chunk,
        )
        for tokens, chunk in docs
        if tokens
    ]

    def retrieve(query: str) -> list[Retrieved]:
        query_terms = set(_terms(query))
        hits: list[Retrieved] = []
        for freq, length, chunk in scored_docs:
            score = 0.0
            for token in query_terms:
                tf = freq.get(token)
                if not tf:
                    continue
                score += idf.get(token, 0.0) * tf * (k1 + 1) / (
                    tf + k1 * (1 - b + b * length / (avg_len or 1.0))
                )
            if score > 0:
                hits.append(Retrieved(key=chunk["chunk_id"], text=chunk["text"], score=score))
        # score desc, then chunk_id so an equal-score tie never depends on dict order.
        hits.sort(key=lambda h: (-h.score, h.key))
        return hits[:top_k]

    return retrieve


# --------------------------------------------------------------------------- #
# Scoring driver.
# --------------------------------------------------------------------------- #
def evaluate_corpus(
    corpus: GoldenCorpus,
    retrieve: Retriever,
    *,
    thresholds: Thresholds | None = None,
    check_grounding: bool = True,
    top_k: int | None = None,
) -> EvalResult:
    """Run every query through ``retrieve`` and score chunk-level metrics."""
    limits = thresholds or Thresholds()
    metrics: list[QueryMetrics] = []
    cited: list[Retrieved] = []
    all_relevant: list[str] = []
    ungrounded: list[str] = []
    for query in corpus.queries:
        hits = list(retrieve(query.query))
        if top_k:
            hits = hits[:top_k]
        metrics.append(
            evaluate_query(query.query_id, query.relevant, [h.key for h in hits], k=query.k)
        )
        # 注入给模型的只有前 k 条，引用面板也只可能来自这前 k 条。
        cited.extend(hits[: query.k])
        all_relevant.extend(query.relevant)
    if check_grounding:
        ungrounded = list(ungrounded_citations([(h.key, h.text) for h in cited], corpus.texts))
    summary = summarize(metrics)
    summary["citation_accuracy"] = round(
        citation_precision([h.key for h in cited], all_relevant), 4
    )
    summary["grounded"] = round(
        1.0 if not cited else 1.0 - len(set(ungrounded)) / len(cited), 4
    )
    failures = _check_thresholds(summary, limits, check_grounding=check_grounding)
    return EvalResult(
        summary=summary, metrics=tuple(metrics), ungrounded=tuple(ungrounded), failures=failures
    )


def _check_thresholds(
    summary: dict[str, Any], limits: Thresholds, *, check_grounding: bool
) -> list[str]:
    if not summary.get("queries"):
        return ["golden set 没有任何查询被评测"]
    checks: list[tuple[str, float, float]] = [
        ("recall@k", summary.get("recall", 0.0), limits.recall),
        ("nDCG@k", summary.get("ndcg", 0.0), limits.ndcg),
        ("MRR", summary.get("mrr", 0.0), limits.mrr),
        ("MAP", summary.get("map", 0.0), limits.map),
    ]
    # 阈值为 0 表示这一轴只上报不设卡（见 Thresholds 的说明）。
    if limits.precision > 0:
        checks.append(("precision@k", summary.get("precision", 0.0), limits.precision))
    if limits.citation > 0:
        checks.append(("引用准确率", summary.get("citation_accuracy", 0.0), limits.citation))
    if check_grounding:
        checks.append(("引用溯源干净率", summary.get("grounded", 0.0), limits.grounded))
    return [
        f"{name} {value:.3f} 低于门禁 {floor:.3f}" for name, value, floor in checks if value < floor
    ]


# --------------------------------------------------------------------------- #
# Contracts for the eval runner.
# --------------------------------------------------------------------------- #
def run_rag_retrieval_eval(
    *, corpus: GoldenCorpus | None = None, thresholds: Thresholds | None = None
) -> dict[str, Any]:
    """Offline contract: deterministic, no network, no DB, no model."""
    from app.evals.runner import _result

    try:
        golden = corpus or load_golden_corpus()
        result = evaluate_corpus(golden, bm25_retriever(golden), thresholds=thresholds)
    except Exception as exc:  # a broken golden set must block the gate loudly
        return _result(_CONTRACT, "fail", reason=f"评测未能运行: {exc}")
    s = result.summary
    detail = (
        f"queries={s.get('queries')} recall@k={s.get('recall')} ndcg={s.get('ndcg')} "
        f"mrr={s.get('mrr')} map={s.get('map')} 引用准确率={s.get('citation_accuracy')} "
        f"溯源干净率={s.get('grounded')} 零召回={s.get('zero_recall_queries')}"
    )
    if result.ok:
        return _result(_CONTRACT, "pass", reason=detail)
    return _result(_CONTRACT, "fail", reason=f"{detail}; " + "; ".join(result.failures))


def _live_kb_ids() -> list[uuid.UUID] | None:
    raw = (os.environ.get(_LIVE_ENV) or "").strip()
    if not raw:
        return None
    try:
        return [uuid.UUID(part.strip()) for part in raw.split(",") if part.strip()]
    except ValueError:
        # Configured but unparseable is a broken deployment, not an absent one.
        return []


def run_live_rag_retrieval_eval(
    *, corpus: GoldenCorpus | None = None, top_k: int = 10
) -> dict[str, Any]:
    """Live contract: the real hybrid retriever over a KB seeded with the golden docs.

    Gated on ``EVAL_RAG_KB_IDS`` — a KB that already contains the golden
    documents (names must match ``document_name``). Missing or malformed config
    SKIPs; a configured-but-broken retrieval FAILs, because that is exactly the
    regression this contract exists to catch.
    """
    from app.evals.runner import _result

    kb_ids = _live_kb_ids()
    if kb_ids is None:
        return _result(
            _LIVE_CONTRACT, "skip", kind="live", reason=f"未设置 {_LIVE_ENV}，跳过在线检索评测"
        )
    if not kb_ids:
        return _result(
            _LIVE_CONTRACT, "fail", kind="live", reason=f"{_LIVE_ENV} 配置了但不是合法 UUID 列表"
        )
    try:
        golden = corpus or load_golden_corpus()

        async def _run() -> list[list[Retrieved]]:
            from app.core.database import AsyncSessionLocal
            from app.rag.rag_service import RagService

            service = RagService()
            runs: list[list[Retrieved]] = []
            async with AsyncSessionLocal() as db:
                for query in golden.queries:
                    _ctx, citations = await service.retrieve(
                        db, query.query, kb_ids, top_k=max(top_k, query.k)
                    )
                    runs.append(
                        [
                            Retrieved(
                                key=str(getattr(c, "document_name", "") or ""),
                                text=str(getattr(c, "snippet", "") or ""),
                                score=float(getattr(c, "score", 0.0) or 0.0),
                            )
                            for c in citations
                        ]
                    )
            return runs

        from app.evals.runner import _run_sync

        runs = _run_sync(_run())
        # Scored by document, not by chunk: production chunk ids are generated at
        # index time, so the only stable label a live KB can match is the filename
        # the golden document was uploaded under.
        scored = [
            evaluate_query(
                query.query_id,
                query.relevant_document_names,
                [hit.key for hit in hits],
                k=query.k,
            )
            for query, hits in zip(golden.queries, runs, strict=True)
        ]
    except Exception as exc:
        return _result(_LIVE_CONTRACT, "fail", kind="live", reason=f"在线评测未能运行: {exc}")

    aggregate = summarize(scored)
    floor = float(os.environ.get("EVAL_RAG_MIN_RECALL", "0.8") or 0.8)
    detail = (
        f"kb_ids={len(kb_ids)} 文档级 recall@k={aggregate.get('recall')} "
        f"ndcg={aggregate.get('ndcg')} mrr={aggregate.get('mrr')}"
    )
    if float(aggregate.get("recall") or 0.0) < floor:
        return _result(
            _LIVE_CONTRACT,
            "fail",
            kind="live",
            reason=f"{detail}; 低于 EVAL_RAG_MIN_RECALL={floor}",
        )
    return _result(_LIVE_CONTRACT, "pass", kind="live", reason=detail)
