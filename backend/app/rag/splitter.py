"""Token-aware recursive text splitter.

Splits long documents into overlapping chunks that stay near ``chunk_size`` tokens
(using a real tokenizer when available, with a character heuristic fallback).
Splitting prefers paragraph -> sentence -> word boundaries so chunks don't cut
mid-sentence more than necessary.
"""
from __future__ import annotations

from typing import Iterable

import tiktoken

from app.core.config import get_settings
from app.rag.base import TextSplitter

# Cheap separators, tried in order of preference (most natural first).
_SEPARATORS: tuple[str, ...] = ("\n\n\n", "\n\n", "\n", ". ", "。", " ", "")


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
        # Fast path: already small enough.
        if self.count_tokens(text) <= self.chunk_size:
            return [text.strip()]

        chunks: list[str] = []
        self._split_text(text, _SEPARATORS, chunks)
        # Apply overlap between consecutive chunks for context continuity.
        if self.chunk_overlap and len(chunks) > 1:
            chunks = self._apply_overlap(chunks)
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
