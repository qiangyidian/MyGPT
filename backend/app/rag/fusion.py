"""Hybrid fusion + context compression (Phase 2).

  * ``rrf_fuse`` — Reciprocal Rank Fusion of vector + keyword hits. Robust to
    different score scales (cosine vs term-frequency) by ranking only.
  * ``compress_context`` — drop near-duplicate chunks (high word overlap) so the
    final context is diverse and within budget.

Both are pure functions over :class:`SearchHit`; nothing here reaches into the
vector store or DB.
"""
from __future__ import annotations

from app.rag.base import SearchHit


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
        for rank, h in enumerate(hits):
            scores[h.id] = scores.get(h.id, 0.0) + 1.0 / (k + rank)
            payload.setdefault(h.id, dict(h.payload))
            # Carry the originating retriever + raw scores for debugging. Collect
            # every distinct retriever that surfaced this hit (the old
            # ``"retrievers" not in p`` guard stopped after the first, so a hit
            # found by both vector AND keyword only ever recorded one tag).
            p = payload[h.id]
            if "retriever" in h.payload:
                tag = h.payload["retriever"]
                cur = p.setdefault("retrievers", [])
                if tag not in cur:
                    cur.append(tag)
    merged = [
        SearchHit(id=i, score=s, payload=payload[i]) for i, s in scores.items()
    ]
    merged.sort(key=lambda x: x.score, reverse=True)
    return merged


def _word_set(text: str) -> set[str]:
    return {w for w in (text or "").lower().split() if w}


def compress_context(hits: list[SearchHit], *, jaccard_threshold: float = 0.85) -> list[SearchHit]:
    """Drop near-duplicate chunks (high word-Jaccard with an already-kept hit)."""
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
