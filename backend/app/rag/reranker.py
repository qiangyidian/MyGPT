"""Rerankers + factory.

``NoopReranker`` preserves today's pass-through behaviour (score order). A real
cross-encoder plugs in behind the same ``Reranker`` interface via
``make_reranker(settings)``:

  * ``noop``      — pass-through (default, zero deps).
  * ``local_bge`` — local sentence-transformers cross-encoder (lazy import; if
    the lib is missing it degrades to NoopReranker with a warning so RAG never
    hard-fails on an optional dependency).
  * ``remote_api``— POST to an OpenAI-compatible/Jina-style /rerank endpoint
    over httpx with a timeout.

All rerankers record ``hit.rerank_score`` (None for noop) and re-sort by it;
``hit.score`` keeps the original vector similarity so the UI can distinguish
the two in debug mode.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.config import get_settings
from app.rag.base import Reranker, SearchHit

logger = logging.getLogger(__name__)


class NoopReranker(Reranker):
    """Pass-through reranker: preserves the vector store's score ordering."""

    kind = "noop"

    async def rerank(self, query: str, hits: list[SearchHit], top_k: int = 5) -> list[SearchHit]:
        # Keep the strongest-scoring hits first; trim to top_k.
        ordered = sorted(hits, key=lambda h: h.score, reverse=True)
        return ordered[:top_k]


class LocalBgeReranker(Reranker):
    """Local cross-encoder via sentence-transformers (lazy, optional dep)."""

    kind = "local_bge"

    def __init__(self, model: str) -> None:
        self._model_name = model
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            from sentence_transformers import CrossEncoder  # type: ignore

            self._model = CrossEncoder(self._model_name)
        return self._model

    async def rerank(self, query: str, hits: list[SearchHit], top_k: int = 5) -> list[SearchHit]:
        if not hits:
            return []
        import asyncio

        pairs = [(query, (h.payload.get("text") or h.payload.get("content") or "")) for h in hits]

        def _score() -> list[float]:
            ce = self._load()
            return ce.predict(pairs).tolist()

        try:
            scores = await asyncio.to_thread(_score)
        except Exception as exc:  # noqa: BLE001 — degrade to score-order
            logger.warning("local bge rerank failed, falling back to score order: %s", exc)
            return sorted(hits, key=lambda h: h.score, reverse=True)[:top_k]

        for h, s in zip(hits, scores):
            h.rerank_score = float(s)
        ordered = sorted(hits, key=lambda h: (h.rerank_score or 0.0), reverse=True)
        return ordered[:top_k]


class RemoteApiReranker(Reranker):
    """Rerank via a remote /rerank endpoint (Jina/Cohere/OpenAI-compatible)."""

    kind = "remote_api"

    def __init__(self, *, base_url: str, api_key: str, model: str = "", timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    async def rerank(self, query: str, hits: list[SearchHit], top_k: int = 5) -> list[SearchHit]:
        if not hits:
            return []
        import asyncio
        import httpx

        documents = [h.payload.get("text") or h.payload.get("content") or "" for h in hits]
        payload: dict[str, Any] = {"query": query, "documents": documents, "top_n": top_k}
        if self._model:
            payload["model"] = self._model

        def _post() -> list[float]:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(
                    f"{self._base_url}/rerank",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"} if self._api_key else {},
                )
                resp.raise_for_status()
                data = resp.json()
            # Support {results:[{index, relevance_score}]} or {scores:[...]}.
            results = data.get("results")
            if isinstance(results, list):
                scored = [0.0] * len(documents)
                for r in results:
                    scored[int(r["index"])] = float(r.get("relevance_score", 0.0))
                return scored
            return [float(x) for x in data.get("scores", [])]

        try:
            scores = await asyncio.to_thread(_post)
        except Exception as exc:  # noqa: BLE001 — degrade to score-order
            logger.warning("remote rerank failed, falling back to score order: %s", exc)
            return sorted(hits, key=lambda h: h.score, reverse=True)[:top_k]

        if len(scores) == len(hits):
            for h, s in zip(hits, scores):
                h.rerank_score = float(s)
            ordered = sorted(hits, key=lambda h: (h.rerank_score or 0.0), reverse=True)
            return ordered[:top_k]
        return sorted(hits, key=lambda h: h.score, reverse=True)[:top_k]


def make_reranker(settings: Any = None) -> Reranker:
    """Build the configured reranker. Always returns a usable Reranker."""
    settings = settings or get_settings()
    kind = (getattr(settings, "RERANKER_KIND", "noop") or "noop").strip().lower()
    if kind == "local_bge":
        try:
            return LocalBgeReranker(getattr(settings, "RERANKER_MODEL", "BAAI/bge-reranker-base"))
        except Exception as exc:  # noqa: BLE001 — optional dep
            logger.warning("local_bge reranker unavailable, using noop: %s", exc)
            return NoopReranker()
    if kind == "remote_api":
        base = getattr(settings, "RERANKER_API_BASE_URL", "")
        if not base:
            logger.warning("remote_api reranker configured without RERANKER_API_BASE_URL; using noop")
            return NoopReranker()
        return RemoteApiReranker(
            base_url=base,
            api_key=getattr(settings, "RERANKER_API_KEY", ""),
            model=getattr(settings, "RERANKER_MODEL", ""),
        )
    return NoopReranker()
