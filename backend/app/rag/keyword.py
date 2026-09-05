"""Keyword retriever (Phase 2 hybrid retrieval).

A lexical retriever over ``DocumentChunk`` rows for a knowledge base. Scores
chunks by normalized term frequency of the query tokens. Pure SQL candidates +
Python scoring so it works on both SQLite (tests) and Postgres (prod) without a
specialized BM25 index. The output shape matches vector ``SearchHit`` so RRF
fusion and citation rendering treat both uniformly.
"""
from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentChunk
from app.rag.base import SearchHit

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
# Cap candidates so scoring stays cheap on large KBs (fuse + rerank trim later).
_MAX_CANDIDATES = 400


def _tokenize(text: str) -> list[str]:
    return [t for t in (w.lower() for w in _TOKEN_RE.findall(text or "")) if len(t) > 1]


def _escape_ilike(term: str) -> str:
    """Escape SQL LIKE wildcards so a query token matches literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class KeywordRetriever:
    """BM25-ish lexical retriever over DocumentChunk (per KB)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def retrieve(
        self,
        query: str,
        kb_id: uuid.UUID,
        top_k: int = 5,
    ) -> list[SearchHit]:
        terms = _tokenize(query)
        if not terms:
            return []
        term_set = set(terms)
        try:
            # Push term matching into SQL so recall is NOT biased to an oldest
            # slice: only chunks containing at least one (escaped) query term are
            # candidates. The cap bites only on very large matching sets, instead
            # of systematically dropping every document uploaded after the first
            # 400 chunks (the old order_by created_at ASC + limit 400 behavior).
            conditions = [
                DocumentChunk.content.ilike("%" + _escape_ilike(t) + "%", escape="\\")
                for t in term_set
            ]
            stmt = (
                select(DocumentChunk, Document)
                .join(Document, Document.id == DocumentChunk.document_id)
                .where(
                    DocumentChunk.knowledge_base_id == kb_id,
                    or_(*conditions),
                )
                .limit(_MAX_CANDIDATES)
            )
            rows = (await self._db.execute(stmt)).all()
        except Exception as exc:
            logger.warning("keyword retrieve failed for kb %s: %s", kb_id, exc)
            return []

        scored: list[tuple[float, DocumentChunk, Document]] = []
        for chunk, doc in rows:
            content = (chunk.content or "").lower()
            tokens = content.split()
            denom = len(tokens) + 1
            # Count whole-token matches, not str.count substrings: str.count would
            # inflate a query term like "cat" inside "catalog", biasing the score.
            hits = sum(tokens.count(t) for t in terms)
            score = hits / denom
            if score > 0:
                scored.append((score, chunk, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        out: list[SearchHit] = []
        for score, chunk, doc in scored[:top_k]:
            out.append(SearchHit(
                id=str(chunk.id),
                score=float(score),
                payload={
                    "document_id": str(doc.id),
                    "document_name": doc.filename,
                    "chunk_id": str(chunk.id),
                    "chunk_index": chunk.chunk_index,
                    "text": chunk.content,
                    "retriever": "keyword",
                },
            ))
        return out
