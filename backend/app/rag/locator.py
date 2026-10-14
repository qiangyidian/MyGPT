"""把 chunk 的字符锚点翻译成「它在原件里的位置」。

输入永远是 ``ParsedDocument``（含 layout）+ chunk 的三个锚点（``page`` /
``char_start`` / ``char_end``），输出是四种 Locator 之一。**不碰 HTTP、不碰
数据库、不读盘** —— 那些是 api 层的事，这个模块要能被纯函数式地测。

挑选顺序是最精确的优先：页内行框 → 表格行 → 幻灯片形状 → 原文文本窗口。
最后那一层是兜底，它对**任何有文本的格式**都成立，所以"定位不了"只发生在
连 char 锚点都没有的老数据上。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.rag.base import ParsedDocument

#: 文本兜底层给出的窗口在命中区间前后各留多少字。太小则用户看不出上下文，
#: 太大则把一个抽屉塞满 —— 800 字约等于两屏。
DEFAULT_TEXT_CONTEXT = 800


@dataclass(frozen=True)
class PageImageLocator:
    kind: str
    page: int
    total_pages: int
    #: 归一化框（相对页面宽高的比例），**一行一个，绝不合并**。
    boxes: tuple[tuple[float, float, float, float], ...]


@dataclass(frozen=True)
class SlideShape:
    rect: tuple[float, float, float, float]
    text: str
    hit: bool


@dataclass(frozen=True)
class SheetRangeLocator:
    kind: str
    sheet: str
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    #: 相对 ``rows`` 的 [起, 止)，表头不算行。
    highlight: tuple[int, int]


@dataclass(frozen=True)
class SlideLocator:
    kind: str
    slide_no: int
    width: float
    height: float
    shapes: tuple[SlideShape, ...]


@dataclass(frozen=True)
class TextRangeLocator:
    kind: str
    text: str
    start: int
    end: int


Locator = PageImageLocator | SheetRangeLocator | SlideLocator | TextRangeLocator


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def _normalise(line: Any, page: Any) -> tuple[float, float, float, float]:
    """行框 → 归一化比例。除零与越界都必须挡住：比例越界前端会画出跑到页外的框。"""
    if page.width <= 0 or page.height <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        _clamp01(line.x0 / page.width),
        _clamp01(line.y0 / page.height),
        _clamp01(line.x1 / page.width),
        _clamp01(line.y1 / page.height),
    )


def _locate_page(
    parsed: ParsedDocument, page: int | None, start: int, end: int
) -> PageImageLocator | None:
    if page is None:
        return None
    layout = parsed.layout
    # 总页数优先取解析器报的文档页数：有布局的页可能少于总页数（扫描页没有
    # 坐标），用 len(layout.pages) 会把"第 34 页 / 共 12 页"这种谎话发给用户。
    total = parsed.metadata.get("pages") if isinstance(parsed.metadata, dict) else None
    total_pages = int(total) if isinstance(total, int) and total > 0 else len(layout.pages)
    for page_lines in layout.pages:
        if page_lines.page != page:
            continue
        hits = [ln for ln in page_lines.lines if ln.char_start < end and ln.char_end > start]
        if not hits:
            return None
        return PageImageLocator(
            kind="page_image",
            page=page,
            total_pages=total_pages,
            boxes=tuple(_normalise(ln, page_lines) for ln in hits),
        )
    return None


def _row_cells(line: str, columns: int) -> tuple[str, ...]:
    cells = [c.strip() for c in line.split(" | ")]
    return tuple((cells + [""] * columns)[:columns])


def _locate_sheet(
    parsed: ParsedDocument, start: int, end: int
) -> SheetRangeLocator | None:
    layout = parsed.layout
    # 表格的块文本按 sheet 顺序排列：``layout.sheets[i]`` 对应 ``blocks[i]``。
    blocks = parsed.tables or parsed.pages or []
    for index, sheet in enumerate(layout.sheets):
        rows = sheet.rows
        if not rows or index >= len(blocks):
            continue
        if not (rows[0].char_start <= start < rows[-1].char_end):
            continue
        hits = [r for r in rows if r.char_start < end and r.char_end > start]
        if not hits:
            return None
        lines = blocks[index].splitlines()
        if sheet.header_line >= len(lines):
            return None
        columns = len(lines[sheet.header_line].split(" | "))
        return SheetRangeLocator(
            kind="sheet_range",
            sheet=sheet.name,
            header=_row_cells(lines[sheet.header_line], columns),
            rows=tuple(_row_cells(ln, columns) for ln in lines[sheet.header_line + 1 :]),
            # 数据行号就是 body 下标，不需要再减表头。
            highlight=(hits[0].row, hits[-1].row + 1),
        )
    return None


def _locate_slide(parsed: ParsedDocument, start: int, end: int) -> SlideLocator | None:
    for slide in parsed.layout.slides:
        shapes = slide.shapes
        if not shapes:
            continue
        if not (shapes[0].char_start <= start < shapes[-1].char_end):
            continue
        if not any(s.char_start < end and s.char_end > start for s in shapes):
            return None
        return SlideLocator(
            kind="slide",
            slide_no=slide.slide_no,
            width=slide.width,
            height=slide.height,
            shapes=tuple(
                SlideShape(
                    rect=(s.x, s.y, s.w, s.h),
                    text=s.text,
                    hit=s.char_start < end and s.char_end > start,
                )
                for s in shapes
            ),
        )
    return None


def locate(
    parsed: ParsedDocument,
    *,
    page: int | None,
    char_start: int | None,
    char_end: int | None,
    text_context: int = DEFAULT_TEXT_CONTEXT,
) -> Locator | None:
    """chunk 的锚点 → Locator。定位不了返回 ``None``（调用方据此回 ``no_locator``）。"""
    if char_start is None or char_end is None or char_end <= char_start:
        return None
    text = parsed.text or ""
    if char_end > len(text):
        return None

    layout = parsed.layout
    if layout is not None and not layout.empty:
        hit = _locate_page(parsed, page, char_start, char_end)
        if hit is not None:
            return hit
        hit = _locate_sheet(parsed, char_start, char_end)
        if hit is not None:
            return hit
        hit = _locate_slide(parsed, char_start, char_end)
        if hit is not None:
            return hit

    low = max(0, char_start - text_context)
    high = min(len(text), char_end + text_context)
    if high <= low:
        return None
    return TextRangeLocator(
        kind="text_range", text=text[low:high], start=char_start - low, end=char_end - low
    )
