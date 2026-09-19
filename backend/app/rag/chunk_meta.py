"""Where a chunk came from inside the document it was cut from.

The splitter returns bare strings, so everything that made a chunk *this* chunk
— its character span, which page it sat on, which heading introduced it — was
throwaway. That left citations able to name a document but not a place in it,
and the parser's own per-page structure on the floor (see
``app/rag/parsers.py``, which builds ``ParsedDocument.pages`` and nobody reads).

Nothing here is exact science: chunk texts are recovered by searching forward in
the parsed text, so a chunk that repeats verbatim may be pinned to its earlier
occurrence. That is good enough for a 「第 3 页 · 章节 X」 affordance and it is
deliberately not used for anything that must be precise — the embedded text and
its ``content_sha256`` remain the source of truth.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any
from collections.abc import Sequence

from app.rag.base import ParsedDocument

_HEADING_RE = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True)
class ChunkSpan:
    """One chunk's position in the parsed document (``None`` = not recoverable)."""

    char_start: int | None = None
    char_end: int | None = None
    page: int | None = None
    heading: str | None = None

    def as_metadata(self) -> dict[str, Any]:
        """Sparse on purpose: a chunk that lost its span stores nothing about it."""
        out: dict[str, Any] = {}
        if self.char_start is not None:
            out["char_start"] = self.char_start
            out["char_end"] = self.char_end
        if self.page is not None:
            out["page"] = self.page
        if self.heading:
            out["heading"] = self.heading
        return out


def _offsets(chunks: Sequence[str], text: str) -> list[tuple[int | None, int | None]]:
    """Forward-moving ``find``: chunks arrive in document order, so one pass is enough."""
    out: list[tuple[int | None, int | None]] = []
    cursor = 0
    for chunk in chunks:
        if not chunk:
            out.append((None, None))
            continue
        start = text.find(chunk, cursor)
        if start < 0:
            # Overlap can glue a previous tail onto a chunk that is not a
            # contiguous slice; fall back to a whole-text search before giving up.
            start = text.find(chunk)
        if start < 0:
            out.append((None, None))
            continue
        out.append((start, start + len(chunk)))
        cursor = start + 1
    return out


def _page_bounds(parsed: ParsedDocument) -> list[tuple[int, int, int]]:
    """``(start, end, page_number)`` per page, 1-based, when pages are recoverable."""
    pages = parsed.pages or []
    text = parsed.text or ""
    bounds: list[tuple[int, int, int]] = []
    cursor = 0
    for number, page in enumerate(pages, start=1):
        if not page:
            continue
        start = text.find(page, cursor)
        if start < 0:
            start = text.find(page)
        if start < 0:
            continue
        bounds.append((start, start + len(page), number))
        cursor = start + 1
    return bounds


def _headings(text: str) -> tuple[list[int], list[str]]:
    """Heading offsets + titles, for the markdown/heading-shaped formats."""
    positions: list[int] = []
    titles: list[str] = []
    for match in _HEADING_RE.finditer(text):
        title = match.group(2).strip()
        if title:
            positions.append(match.start())
            titles.append(title)
    return positions, titles


def _at(offsets: list[int], titles: list[str], position: int) -> str | None:
    if not offsets:
        return None
    index = bisect_right(offsets, position) - 1
    return titles[index] if index >= 0 else None


def annotate(chunks: Sequence[str], parsed: ParsedDocument) -> list[ChunkSpan]:
    """Attach provenance to each chunk, best-effort and in document order."""
    text = parsed.text or ""
    spans = _offsets(chunks, text)
    bounds = _page_bounds(parsed)
    heading_positions, heading_titles = _headings(text)

    out: list[ChunkSpan] = []
    for (start, end) in spans:
        if start is None:
            out.append(ChunkSpan())
            continue
        page = next(
            (number for (p_start, p_end, number) in bounds if p_start <= start < p_end),
            None,
        )
        out.append(
            ChunkSpan(
                char_start=start,
                char_end=end,
                page=page,
                heading=_at(heading_positions, heading_titles, start),
            )
        )
    return out


def chunk_metadata(
    span: ChunkSpan,
    parsed: ParsedDocument,
    *,
    token_count: int,
    sha256: str,
) -> dict[str, Any]:
    """The ``document_chunks.metadata`` row: span + page + heading + parser provenance."""
    meta: dict[str, Any] = {"tokens": token_count, "sha256": sha256}
    meta.update(span.as_metadata())
    parser_meta = parsed.metadata or {}
    for key in ("parser_used", "pages", "ocr_used", "content_kind"):
        value = parser_meta.get(key)
        if value is not None:
            meta[key] = value
    return meta
