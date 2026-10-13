"""Hybrid fusion + context compression (Phase 2).

  * ``rrf_fuse`` — Reciprocal Rank Fusion of vector + keyword hits. Robust to
    different score scales (cosine vs term-frequency) by ranking only.
  * ``compress_context`` — drop near-duplicate chunks (high word overlap) so the
    final context is diverse and within budget.

Both are pure functions over :class:`SearchHit`; nothing here reaches into the
vector store or DB.
"""
from __future__ import annotations

import re

from app.rag.base import SearchHit

_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")
_WORD_RE = re.compile(r"[\w]+", re.UNICODE)


def rrf_fuse(
    *ranked_lists: list[SearchHit], k: int = 60
) -> list[SearchHit]:
    """Reciprocal Rank Fusion. Each input list is assumed best-first.

    Returns a single deduplicated, fusion-score-sorted list. Payloads from the
    first list to see a hit win (vector preferred on ties).
    """
    scores: dict[str, float] = {}
    payload: dict[str, dict] = {}
    for hits in ranked_lists:
        # RRF ranks are one-based: the best result contributes 1 / (k + 1).
        for rank, h in enumerate(hits, start=1):
            scores[h.id] = scores.get(h.id, 0.0) + 1.0 / (k + rank)
            payload.setdefault(h.id, dict(h.payload))
            # Preserve per-channel evidence for retrieval diagnostics while the
            # SearchHit score remains the fused RRF score.
            p = payload[h.id]
            tag = str(h.payload.get("retriever") or "unknown")
            channels = p.setdefault("retrieval_channels", {})
            channels[tag] = {"rank": rank, "raw_score": float(h.score)}
            cur = p.setdefault("retrievers", [])
            if tag not in cur:
                cur.append(tag)
    merged = [
        SearchHit(id=i, score=s, payload=payload[i]) for i, s in scores.items()
    ]
    merged.sort(key=lambda x: x.score, reverse=True)
    return merged


def _word_set(text: str) -> set[str]:
    """Tokenize Latin words and CJK bigrams for language-aware deduplication."""
    value = (text or "").lower()
    cjk_runs = _CJK_RUN_RE.findall(value)
    # 单字符拉丁词通常只是噪声，但要**放行单独的数字**：`0` / `1` 这种编号往往是两块
    # 唯一的区别（列表序号、选项标签、步骤号）。一律丢掉会把「kb_1 chunk 0」和
    # 「kb_1 chunk 1」变成同一个词集，相似度 1.0，于是其中一块被当作重复从上下文里
    # 静默删掉——用户看到的是「检索到了却像没检索到」。混合词（`kb_1`）本来就被
    # ``\\w+`` 整体保留，这条只影响独立单字符。
    terms = {
        word
        for part in _CJK_RUN_RE.split(value)
        for word in _WORD_RE.findall(part)
        if len(word) > 1 or word.isdigit()
    }
    for run in cjk_runs:
        if len(run) == 1:
            terms.add(run)
        else:
            terms.update(run[index:index + 2] for index in range(len(run) - 1))
    return terms


def compress_context(hits: list[SearchHit], *, jaccard_threshold: float = 0.85) -> list[SearchHit]:
    """Drop near-duplicate chunks using Latin words and CJK bigrams."""
    kept: list[SearchHit] = []
    kept_sets: list[set[str]] = []
    for h in hits:
        text = h.payload.get("text") or h.payload.get("content") or ""
        ws = _word_set(text)
        if not ws:
            # Empty/whitespace chunk carries no signal — drop it (the old code
            # skipped dedup for these, so every empty chunk survived).
            continue
        duplicate = False
        for ks in kept_sets:
            inter = len(ws & ks)
            union = len(ws | ks) or 1
            if inter / union >= jaccard_threshold:
                duplicate = True
                break
        if not duplicate:
            kept.append(h)
            kept_sets.append(ws)
    return kept
