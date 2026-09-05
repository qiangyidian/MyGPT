"""Token-aware recursive text splitter.

Splits long documents into overlapping chunks that stay near ``chunk_size`` tokens
(using a real tokenizer when available, with a character heuristic fallback).
Splitting prefers paragraph -> sentence -> word boundaries so chunks don't cut
mid-sentence more than necessary.
"""
from __future__ import annotations

import re

import tiktoken

from app.core.config import get_settings
from app.rag.base import TextSplitter

# Cheap separators, tried in order of preference (most natural first).
# Markdown headings split FIRST so each section becomes its own chunk even
# when the whole document fits in one chunk_size — retrieval needs topical
# granularity, not just size capping.
_SEPARATORS: tuple[str, ...] = (
    "\n\n\n",
    "\n# ",      # markdown h1
    "\n## ",     # markdown h2
    "\n### ",    # markdown h3
    "\n\n",
    "\n",
    ". ",
    "。",
    " ",
    "",
)


def _count_tokens(text: str, model: str) -> int:
    if not text:
        return 0
    try:
        enc = tiktoken.encoding_for_model(model)
    except Exception:
        try:
            enc = tiktoken.get_encoding("cl100k_base")
        except Exception:
            return max(1, len(text) // 4)
    try:
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 4)


class RecursiveTextSplitter(TextSplitter):
    """Greedy recursive splitter with overlap."""

    def __init__(
        self,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
        model: str = "gpt-3.5-turbo",
    ) -> None:
        settings = get_settings()
        self.chunk_size = chunk_size or settings.RAG_CHUNK_SIZE
        self.chunk_overlap = chunk_overlap or settings.RAG_CHUNK_OVERLAP
        self.model = model
        if self.chunk_overlap >= self.chunk_size:
            # Overlap must be smaller than the chunk or the recursion never ends.
            self.chunk_overlap = max(0, self.chunk_size // 4)

    def count_tokens(self, text: str) -> int:
        return _count_tokens(text, self.model)

    def split(self, text: str) -> list[str]:
        if not text or not text.strip():
            return []
        # Markdown sections are TOPICAL units: split each heading section into
        # its own chunk even when the whole file would fit in one chunk_size.
        # A small mixed-topic file as a single chunk defeats retrieval — a hit
        # returns every topic at once and scores blur across sections.
        # Overlap is applied WITHIN a section's sub-chunks only: consecutive
        # sections are different topics, so gluing their text together via
        # overlap would only blur the vectors again.
        sections = self._split_markdown_sections(text)
        if sections:
            out: list[str] = []
            for sec in sections:
                parts = self._split_oversized(sec)
                if self.chunk_overlap and len(parts) > 1:
                    parts = self._apply_overlap(parts)
                out.extend(parts)
            return [c for c in out if c and c.strip()]
        # Plain text: structural boundaries (paragraph → sentence → word).
        chunks: list[str] = []
        self._split_text(text, _SEPARATORS, chunks)
        if self.chunk_overlap and len(chunks) > 1:
            chunks = self._apply_overlap(chunks)
        return [c for c in chunks if c and c.strip()]

    _HEADING_RE = re.compile(r"(?m)^#{1,6}\s+.+$")

    def _split_markdown_sections(self, text: str) -> list[str]:
        """Split into per-heading sections. Empty when there are < 2 headings
        (a single heading adds no topical structure worth splitting on)."""
        matches = list(self._HEADING_RE.finditer(text))
        if len(matches) < 2:
            return []
        sections: list[str] = []
        # Preamble before the first heading (if any).
        if matches[0].start() > 0 and text[: matches[0].start()].strip():
            sections.append(text[: matches[0].start()].strip())
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            sections.append(text[m.start():end].strip())
        return [s for s in sections if s]

    def _split_oversized(self, text: str) -> list[str]:
        """Chunk one section further only when it exceeds chunk_size."""
        if self.count_tokens(text) <= self.chunk_size:
            return [text]
        chunks: list[str] = []
        self._split_text(text, _SEPARATORS, chunks)
        return [c for c in chunks if c and c.strip()]

    def _split_text(self, text: str, separators: tuple[str, ...], out: list[str]) -> None:
        sep = separators[-1]
        new_separators: tuple[str, ...] = ()
        for idx, candidate in enumerate(separators):
            if candidate == "":
                sep = ""
                break
            if candidate in text:
                sep = candidate
                new_separators = separators[idx + 1:]
                break

        parts = text.split(sep) if sep else list(text)
        # Merge small parts greedily into chunk_size-ish blocks.
        buf = ""
        for part in parts:
            piece = (part + sep) if sep else part
            if self.count_tokens(buf + piece) <= self.chunk_size:
                buf += piece
                continue
            if buf.strip():
                out.append(buf.strip())
            # If a single piece is still too big, recurse with finer separators.
            if self.count_tokens(piece) > self.chunk_size and new_separators:
                self._split_text(piece, new_separators, out)
                buf = ""
            else:
                buf = piece
        if buf.strip():
            out.append(buf.strip())

    def _apply_overlap(self, chunks: list[str]) -> list[str]:
        result: list[str] = []
        for i, chunk in enumerate(chunks):
            if i == 0:
                result.append(chunk)
                continue
            prev = result[-1]
            tail = self._take_tail_tokens(prev, self.chunk_overlap)
            result.append((tail + chunk) if tail else chunk)
        return result

    def _take_tail_tokens(self, text: str, n_tokens: int) -> str:
        # Approximate tail by characters proportional to n_tokens; good enough
        # for overlap continuity without re-tokenising both halves.
        if n_tokens <= 0 or not text:
            return ""
        approx_chars = n_tokens * 4
        return text[-approx_chars:] if len(text) > approx_chars else text
