"""文档的版面索引：把 chunk 的字符区间翻译成「页面上的哪一行」的原料。

只描述数据，不做定位（定位在 ``app/rag/locator.py``）。每个布局单元都必须带
它在 ``ParsedDocument.text`` 里的字符区间 —— 那是与 ``chunk_meta`` 存下的
``char_start/char_end`` 对齐的唯一接口。

坐标一律 **左上角原点、y 向下、单位 pt**（PyMuPDF 原生坐标系），与 PNG 像素
同向。若将来接入左手原点的解析器（如 Docling 的 BOTTOMLEFT），必须在写进
layout 之前翻好：两套坐标系流进同一个 dict 会静默产出上下颠倒的框，而那种错误
在测试里看起来只是"框位置有点怪"。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LineBox:
    """一行文本、它在页面上的框、它在 ``ParsedDocument.text`` 里的字符区间。"""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    char_start: int
    char_end: int


@dataclass(frozen=True)
class PageLines:
    """一页的所有文本行。``page`` 是 1-based，与 chunk metadata 的 page 同一套编号。"""

    page: int
    width: float
    height: float
    lines: tuple[LineBox, ...]


@dataclass(frozen=True)
class SheetRow:
    """表格里的一行。``row`` 是 0-based，0 是表头行。"""

    row: int
    char_start: int
    char_end: int


@dataclass(frozen=True)
class SheetLayout:
    name: str
    rows: tuple[SheetRow, ...]


@dataclass(frozen=True)
class ShapeBox:
    """幻灯片上的一个形状（pt 为单位，原点为幻灯片左上角）。"""

    x: float
    y: float
    w: float
    h: float
    text: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class SlideLayout:
    slide_no: int
    width: float
    height: float
    shapes: tuple[ShapeBox, ...]


@dataclass(frozen=True)
class DocumentLayout:
    """一份文档的版面索引。三种集合互斥：解析器只填它有把握的那一种。"""

    pages: tuple[PageLines, ...] = ()
    sheets: tuple[SheetLayout, ...] = ()
    slides: tuple[SlideLayout, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.pages or self.sheets or self.slides)


def page_bases(pages: list[str]) -> dict[int, int]:
    """每页（1-based）在 ``"\\n\\n".join(非空页)`` 之后的起始偏移。

    空页被 join 过滤掉，所以页号与文本位置不是 1:1 —— 靠 ``find`` 反推会在
    重复页（目录页、模板页）上命中错误的副本。这里直接把 join 的过程走一遍，
    偏移是算出来的而不是猜出来的。
    """
    bases: dict[int, int] = {}
    cursor = 0
    first = True
    for number, text in enumerate(pages, start=1):
        if not text:
            continue
        if not first:
            cursor += 2  # "\n\n"
        bases[number] = cursor
        cursor += len(text)
        first = False
    return bases


def pdf_lines(page: Any, page_text: str, base: int) -> list[LineBox]:
    """一页的文本行 → ``LineBox`` 列表。``base`` 是该页在全文里的起始偏移。

    行文本取自 ``get_text("dict")`` 的 blocks→lines→spans；行在页文本里的偏移
    用**前向 search** 恢复，不依赖「dict 重建等于 text 模式」这个等式成立 ——
    真实 PDF 的连字、双栏、页眉会把那个等式打破。找不到的行**直接丢弃**：
    宁可不给高亮，也不给一个错位的框。
    """
    try:
        raw = page.get_text("dict")
    except Exception:  # pragma: no cover - 损坏页
        return []
    out: list[LineBox] = []
    cursor = 0
    for block in raw.get("blocks", []) or []:
        if block.get("type") != 0:  # 图片块没有文本行
            continue
        for line in block.get("lines", []) or []:
            text = "".join(span.get("text", "") for span in line.get("spans", []) or [])
            if not text.strip():
                continue
            at = page_text.find(text, cursor)
            if at < 0:
                at = page_text.find(text)
            if at < 0:
                continue
            bbox = line.get("bbox") or ()
            if len(bbox) != 4:
                continue
            out.append(
                LineBox(
                    text=text,
                    x0=float(bbox[0]),
                    y0=float(bbox[1]),
                    x1=float(bbox[2]),
                    y1=float(bbox[3]),
                    char_start=base + at,
                    char_end=base + at + len(text),
                )
            )
            cursor = at + 1
    return out
