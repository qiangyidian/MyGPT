"""信息检索指标（纯函数，不碰任何存储）。

评测要能被门禁引用，前提是所有指标只吃「排序后的 id 序列 + 相关 id 集合」这种
最小输入：同一份 golden set 换一个检索器就能直接比涨跌，指标本身不需要 Qdrant、
数据库或模型。

约定（与 IR 社区的常规做法一致）：
- 相关性和 ``k`` 一起决定分母：``recall@k`` 是「前 k 里命中的相关块 / 全部相关块」，
  所以一个块都召回不来时是 0，而不是「无法计算」。
- nDCG 用二值增益（相关 1、不相关 0），理想排序就是把相关块顶到最前。
- MRR/AP 在整个结果集上算，不截断到 ``k``：它们衡量的正是「首个正确答案排第几」。
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "QueryMetrics",
    "citation_precision",
    "evaluate_query",
    "hits_at_k",
    "mean_reciprocal_rank",
    "ndcg_at_k",
    "precision_at_k",
    "recall_at_k",
    "summarize",
    "ungrounded_citations",
]


def hits_at_k(ranked: Sequence[str], relevant: Sequence[str], k: int) -> int:
    """相关块里真正进入前 ``k`` 的个数（重复 id 只算一次）。"""
    rel = set(relevant)
    return len({cid for cid in ranked[: max(0, int(k))] if cid in rel})


def recall_at_k(ranked: Sequence[str], relevant: Sequence[str], k: int) -> float:
    if not relevant:
        # 没有标注正确答案的查询不该混进 golden set；真出现时按 0 计，
        # 免得「空标注 = 满分」把指标悄悄刷高。
        return 0.0
    return hits_at_k(ranked, relevant, k) / len(set(relevant))


def precision_at_k(ranked: Sequence[str], relevant: Sequence[str], k: int) -> float:
    window = ranked[: max(0, int(k))]
    if not window:
        return 0.0
    rel = set(relevant)
    return len({cid for cid in window if cid in rel}) / len(window)


def mean_reciprocal_rank(ranked: Sequence[str], relevant: Sequence[str]) -> float:
    rel = set(relevant)
    for position, cid in enumerate(ranked, start=1):
        if cid in rel:
            return 1.0 / position
    return 0.0


def _average_precision(ranked: Sequence[str], relevant: Sequence[str]) -> float:
    """AP = 每个命中点上的 precision 平均（二值相关）。"""
    rel = set(relevant)
    if not rel:
        return 0.0
    hits = 0
    total = 0.0
    seen: set[str] = set()
    for position, cid in enumerate(ranked, start=1):
        if cid in seen:
            continue
        seen.add(cid)
        if cid not in rel:
            continue
        hits += 1
        total += hits / position
    denominator = min(len(rel), len(seen))
    return total / denominator if denominator else 0.0


def dcg_at_k(ranked: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """二值增益 DCG：命中位置越靠前，贡献越大（log2 折扣）。"""
    rel = set(relevant)
    seen: set[str] = set()
    score = 0.0
    for position, cid in enumerate(ranked[: max(0, int(k))], start=1):
        if cid in seen or cid not in rel:
            continue
        seen.add(cid)
        score += 1.0 / math.log2(position + 1)
    return score


def ndcg_at_k(ranked: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """归一化 DCG，1.0 = 相关块全部按最优顺序排在前 k。"""
    rel = set(relevant)
    if not rel:
        return 0.0
    ideal_window = min(len(rel), max(0, int(k)))
    ideal = sum(1.0 / math.log2(position + 1) for position in range(1, ideal_window + 1))
    if ideal <= 0:
        return 0.0
    return dcg_at_k(ranked, rel, k) / ideal


def citation_precision(cited: Sequence[str], relevant: Sequence[str]) -> float:
    """引用准确率：模型实际被要求引用的片段里，有多少确实答得上这个问题。"""
    return precision_at_k(cited, relevant, len(cited))


def ungrounded_citations(
    cited: Sequence[tuple[str, str]],
    corpus_text: dict[str, str],
    is_snippet_of: Callable[[str, str], bool] = lambda snippet, full: snippet in full,
) -> list[str]:
    """返回「引用了却没在自己声称的那块里」的 chunk id。

    引用面板会把 snippet 归到某个 chunk 上；只要 snippet 不是那块的内容，用户点
    「查看来源」就会看到对不上的原文——这是溯源链断裂，跟检索准不准无关，所以
    单独判、且要求 100% 干净。
    """
    bad: list[str] = []
    for chunk_id, snippet in cited:
        full = corpus_text.get(chunk_id)
        if full is None:
            bad.append(chunk_id)
            continue
        if snippet and not is_snippet_of(snippet, full):
            bad.append(chunk_id)
    return bad


@dataclass(frozen=True)
class QueryMetrics:
    """一次查询在某截断点上的全部指标。"""

    query_id: str
    k: int
    relevant: tuple[str, ...]
    retrieved: tuple[str, ...]
    recall: float
    precision: float
    ndcg: float
    mrr: float
    average_precision: float
    first_relevant_rank: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "k": self.k,
            "relevant_count": len(self.relevant),
            "retrieved_count": len(self.retrieved),
            "recall": round(self.recall, 4),
            "precision": round(self.precision, 4),
            "ndcg": round(self.ndcg, 4),
            "mrr": round(self.mrr, 4),
            "average_precision": round(self.average_precision, 4),
            "first_relevant_rank": self.first_relevant_rank,
        }


def evaluate_query(
    query_id: str,
    relevant: Sequence[str],
    ranked: Sequence[str],
    *,
    k: int,
) -> QueryMetrics:
    rel = list(dict.fromkeys(relevant))
    order = list(ranked)
    first = next((i for i, cid in enumerate(order, start=1) if cid in set(rel)), None)
    return QueryMetrics(
        query_id=query_id,
        k=int(k),
        relevant=tuple(rel),
        retrieved=tuple(order),
        recall=recall_at_k(order, rel, k),
        precision=precision_at_k(order, rel, k),
        ndcg=ndcg_at_k(order, rel, k),
        mrr=mean_reciprocal_rank(order, rel),
        average_precision=_average_precision(order, rel),
        first_relevant_rank=first,
    )


def summarize(metrics: Sequence[QueryMetrics]) -> dict[str, Any]:
    """按查询等权平均（每条查询一票，不看它标了多少个相关块）。"""
    if not metrics:
        return {"queries": 0}
    n = len(metrics)

    def mean(pick: Callable[[QueryMetrics], float]) -> float:
        return sum(pick(m) for m in metrics) / n

    return {
        "queries": n,
        "k": max(m.k for m in metrics),
        "recall": round(mean(lambda m: m.recall), 4),
        "precision": round(mean(lambda m: m.precision), 4),
        "ndcg": round(mean(lambda m: m.ndcg), 4),
        "mrr": round(mean(lambda m: m.mrr), 4),
        "map": round(mean(lambda m: m.average_precision), 4),
        "zero_recall_queries": sorted(m.query_id for m in metrics if m.recall == 0.0),
        "per_query": [m.to_dict() for m in metrics],
    }
