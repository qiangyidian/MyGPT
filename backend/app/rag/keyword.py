"""Keyword retriever (Phase 2 hybrid retrieval).

A lexical retriever over ``DocumentChunk`` rows for a knowledge base. Query
terms are Latin words (matched as whole tokens) and CJK bigrams (matched as
substrings); chunks are scored by normalized term frequency. Pure SQL
candidates + Python scoring so it works on both SQLite (tests) and Postgres
(prod) without a specialized BM25 index. The output shape matches vector
``SearchHit`` so RRF fusion and citation rendering treat both uniformly.

Candidate selection stays ``ILIKE`` on every engine, deliberately: Postgres
could answer the same query with ``to_tsvector`` (better ranking, an index that
is easier to build), but full-text matching is token-based, and with no CJK
segmentation an entire Chinese sentence collapses into ONE token — so a query
for 「检索」 would stop matching 「知识库检索」. Substring matching is the recall
this retriever is for, and the bigram expansion is what makes a Chinese query
actually reach the chunks its own SQL selects. The speed problem ILIKE invites
is only partly answered: migration 0015 adds a ``pg_trgm`` GIN index, which a
measured 500k-row table uses for terms of three characters or more (Latin words,
longer Chinese phrases — index-bitmap cost 30 vs a few thousand for the scan)
but NOT for the two-character CJK bigrams that dominate Chinese queries: there
the planner keeps the (cheaper) scan, so those queries scale with the KB's chunk
count and the per-KB btree on ``knowledge_base_id`` is what bounds the work.
"""
from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.like import LIKE_ESCAPE, like_pattern
from app.models import Document, DocumentChunk
from app.rag.base import SearchHit

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_CJK_RUN_RE = re.compile(rf"{_CJK_RE.pattern}+")
# Cap candidates so scoring stays cheap on large KBs (fuse + rerank trim later).
_MAX_CANDIDATES = 400


def _latin_tokens(text: str) -> list[str]:
    """Words from the non-CJK parts of the text.

    Splitting on CJK runs first is what keeps 「AI模型」 contributing ``ai``:
    ``\\w`` is unicode-aware, so the whole mixed string used to arrive as one
    token and get dropped as "CJK".
    """
    return [
        t
        for t in (
            w.lower() for part in _CJK_RUN_RE.split(text or "") for w in _TOKEN_RE.findall(part)
        )
        if len(t) > 1
    ]


def _cjk_grams(text: str) -> list[str]:
    """Bigrams per maximal CJK run (a lone run char stays a unigram).

    Single chars are too noisy on their own and a whole run is too long to ever
    match a reworded chunk; bigrams are the compromise CJK search converges on.
    """
    grams: list[str] = []
    for run in _CJK_RUN_RE.findall(text or ""):
        if len(run) == 1:
            grams.append(run)
        else:
            grams.extend(run[i:i + 2] for i in range(len(run) - 1))
    return grams


def _tokenize(text: str) -> tuple[list[str], list[str]]:
    """``(latin words, CJK grams)`` — the two kinds need different matching."""
    return _latin_tokens(text), _cjk_grams(text)


def _candidate_conditions(terms: Sequence[str]) -> list:
    seen = list(dict.fromkeys(terms))  # dedup, keep order: repeated grams are common
    return [
        DocumentChunk.content.ilike(like_pattern(t), escape=LIKE_ESCAPE) for t in seen
    ]


def _coverage(conditions: Sequence) -> ColumnElement[int]:
    """How many query terms a row contains, as a SQL expression.

    ``CASE`` rather than ``CAST(bool AS INT)`` because Postgres has no
    boolean→integer cast, and this must run on SQLite too.
    """
    parts = [case((c, 1), else_=0) for c in conditions]
    total = parts[0]
    for part in parts[1:]:
        total = total + part
    return total


def _max_candidates() -> int:
    """Python-side scoring bounds CPU per turn, so the cap is a real knob."""
    configured = int(getattr(get_settings(), "RAG_KEYWORD_CANDIDATES", 0) or 0)
    return configured if configured > 0 else _MAX_CANDIDATES


def _score(
    content_lower: str,
    latin_terms: Sequence[str],
    cjk_terms: Sequence[str],
) -> float:
    """Term frequency normalised by content size.

    Latin terms match as *whole tokens* (``cat`` must not count inside
    ``catalog``); CJK grams match as substrings, the only sensible boundary for
    them. Scoring latin-only off ``str.split()`` — as this file used to — made
    every Chinese chunk score 0.0, so the lexical half of hybrid retrieval was
    silently English-only even though its SQL recalled those chunks fine.
    """
    tokens = content_lower.split()
    # One C-level pass: per-character regex matching here would run ~10^5 times
    # per turn on a full candidate window.
    cjk_chars = len(_CJK_RE.findall(content_lower))
    denom = len(tokens) + cjk_chars + 1
    matches = sum(tokens.count(t) for t in latin_terms)
    matches += sum(content_lower.count(t) for t in cjk_terms)
    return matches / denom


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
        latin, cjk = _tokenize(query)
        terms = latin + cjk
        if not terms:
            return []
        try:
            # Push term matching into SQL so recall is NOT biased to an oldest
            # slice: only chunks containing at least one (escaped) query term are
            # candidates. The cap bites only on very large matching sets, and it
            # then drops the chunks matching the *fewest* query terms (a CJK
            # query expands into several grams, so an unordered cap could evict
            # the best chunk for an arbitrary one) — never the oldest rows, as
            # the old ``order_by created_at ASC`` + limit 400 did.
            conditions = _candidate_conditions(terms)
            stmt = (
                select(DocumentChunk, Document)
                .join(Document, Document.id == DocumentChunk.document_id)
                .where(
                    DocumentChunk.knowledge_base_id == kb_id,
                    or_(*conditions),
                )
                .order_by(
                    _coverage(conditions).desc(),
                    DocumentChunk.document_id,
                    DocumentChunk.chunk_index,
                )
                .limit(_max_candidates())
            )
            rows = (await self._db.execute(stmt)).all()
        except Exception as exc:
            logger.warning("keyword retrieve failed for kb %s: %s", kb_id, exc)
            return []

        scored: list[tuple[float, DocumentChunk, Document]] = []
        for chunk, doc in rows:
            content = (chunk.content or "").lower()
            score = _score(content, latin, cjk)
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
                    "page": (chunk.metadata_ or {}).get("page"),
                    "heading": (chunk.metadata_ or {}).get("heading"),
                    "retriever": "keyword",
                },
            ))
        return out
