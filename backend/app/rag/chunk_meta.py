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
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Any
from collections.abc import Sequence

from app.rag.base import ParsedDocument

_HEADING_RE = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")
#: 非空白片段。切块器的接缝会归一空白，所以"对齐"必须按非空白内容来算。
_TOKEN_RE = re.compile(r"\S+")


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


class _CompactIndex:
    """全文去掉空白后的字符序列 + 每个字符在原位置，用于跨接缝对齐。

    为什么按**非空白字符**而不是按 token：切块器的接缝会以任意方式改动空白 ——
    跨页的 ``"\\n\\n"`` 可能变成 ``"\\n"``，也可能整块消失（上一页的最后一行与
    下一页的第一行被直接粘成一个 token）。按 token 对齐对"两个 token 粘成一个"
    无能为力；按非空白字符对齐则对这些情况一视同仁。

    只在精确 ``find`` 失败时才建（懒建），因为它要遍历全文，而绝大多数 chunk
    都在快路径上就命中了。
    """

    __slots__ = ("compact", "positions")

    def __init__(self, text: str) -> None:
        chars: list[str] = []
        positions: list[int] = []
        for i, ch in enumerate(text):
            if ch.isspace():
                continue
            chars.append(ch)
            positions.append(i)
        self.compact = "".join(chars)
        self.positions = positions

    def find(self, chunk: str, from_char: int) -> tuple[int, int] | None:
        """chunk 在原文里的字符区间 [起, 止)。以 ``from_char`` 为搜索起点。

        返回的边界是 chunk **首个/末个非空白字符**的位置：chunk 首尾的空白不属于
        它的内容，把它算进去只会让区间多盖到前一块的尾巴。
        """
        needle = "".join(ch for ch in chunk if not ch.isspace())
        if not needle:
            return None
        at = self.compact.find(needle, bisect_left(self.positions, from_char))
        if at < 0:
            # 重叠窗口会让本块的起点落在上一块之前，退回全文再找一次。
            at = self.compact.find(needle)
        if at < 0:
            return None
        return self.positions[at], self.positions[at + len(needle) - 1] + 1


def _offsets(chunks: Sequence[str], text: str) -> list[tuple[int | None, int | None]]:
    """按文档顺序把每个 chunk 对回原文的字符区间。

    两级策略：

    1. **精确 ``find`` 快路径。** 命中就用 ``start + len(chunk)``，语义与历史行为
       一字不差 —— 已经能定位的 chunk 不受本函数改动的影响。
    2. **忽略空白对齐的慢路径。** 真切块器的输出不是精确切片：重叠窗口跨过分页
       接缝时空白会被改写（``"\\n\\n"`` → ``"\\n"``，甚至整个消失），``find`` 因此
       失败。从前这条路径直接放弃，于是接缝上的 chunk 同时丢掉 char 锚点、页号
       与 heading —— 而那正是引用上「第 N 页」与原文定位的全部输入。
    3. 两级都失败就是 ``None``：**不猜**。真正的"不在原文里"仍然是找不到。
    """
    out: list[tuple[int | None, int | None]] = []
    index: _CompactIndex | None = None
    cursor = 0
    for chunk in chunks:
        if not chunk:
            out.append((None, None))
            continue
        start = text.find(chunk, cursor)
        if start < 0:
            # 重叠会把上一块的尾巴粘进来，使本块不再是连续切片；
            # 退回全文搜索再试一次。
            start = text.find(chunk)
        if start >= 0:
            out.append((start, start + len(chunk)))
            cursor = start + 1
            continue

        if index is None:
            index = _CompactIndex(text)
        span = index.find(chunk, cursor)
        if span is None:
            out.append((None, None))
            continue
        out.append(span)
        cursor = span[0] + 1
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
