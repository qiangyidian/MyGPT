"""定位器：四种 kind 的挑选顺序、归一化，以及一切"定位不了"都必须返回 None。"""
from __future__ import annotations

import pymupdf

from app.rag.base import ParsedDocument
from app.rag.layout import (
    DocumentLayout,
    LineBox,
    PageLines,
    SheetLayout,
    SheetRow,
)
from app.rag.locator import locate
from app.rag.parsers import default_parser


def _pdf(path) -> str:
    doc = pymupdf.open()
    page = doc.new_page()
    for i in range(4):
        page.insert_text((72, 72 + i * 16), f"第{i}行 内容 ABC-{i}", fontsize=10)
    doc.save(str(path))
    doc.close()
    return str(path)


def _sheet_fixture():
    """一份两列四行的表格 + 它的逐行字符区间（表头单列，数据行从 0 编号）。"""
    text = "h1 | h2\nr1 | a\nr2 | b\nr3 | c"
    lines = text.split("\n")
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line) + 1
    rows: list[SheetRow] = []
    for i, line in enumerate(lines[1:]):  # 跳过表头
        rows.append(
            SheetRow(row=i, char_start=offsets[i + 1], char_end=offsets[i + 1] + len(line))
        )
    parsed = ParsedDocument(
        text=text,
        tables=[text],
        layout=DocumentLayout(
            sheets=(SheetLayout(name="Sheet1", header_line=0, rows=tuple(rows)),)
        ),
    )
    return parsed, rows


def test_locate_returns_page_image_for_a_chunk_covered_by_lines(tmp_path):
    parsed = default_parser.parse(_pdf(tmp_path / "a.pdf"), ".pdf")
    page = parsed.layout.pages[0]
    second = page.lines[1]

    loc = locate(
        parsed, page=1, char_start=second.char_start, char_end=second.char_end
    )

    assert loc is not None and loc.kind == "page_image"
    assert loc.page == 1 and loc.total_pages == 1
    assert len(loc.boxes) == 1, "单行 chunk 只应给一个框"
    x0, y0, x1, y1 = loc.boxes[0]
    assert 0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0


def test_locate_never_merges_lines_into_one_box(tmp_path):
    """跨多行的 chunk 必须给多个框 —— 并成一个矩形会把相邻内容也框进去。"""
    parsed = default_parser.parse(_pdf(tmp_path / "b.pdf"), ".pdf")
    page = parsed.layout.pages[0]
    first, last = page.lines[0], page.lines[2]

    loc = locate(parsed, page=1, char_start=first.char_start, char_end=last.char_end)

    assert loc is not None and loc.kind == "page_image"
    assert len(loc.boxes) == 3


def test_locate_falls_back_to_text_range_without_layout():
    """没有版面（Word/TXT 那种）时必须退到文本片段，而不是返回 None。"""
    parsed = ParsedDocument(text="甲" * 3000, layout=None)
    loc = locate(parsed, page=None, char_start=1000, char_end=1100)

    assert loc is not None and loc.kind == "text_range"
    assert loc.text[loc.start : loc.end] == "甲" * 100
    assert len(loc.text) < 3000, "窗口必须带上下文而不是整篇"


def test_locate_returns_none_without_char_anchors():
    """老 chunk 没有 char 偏移 —— 必须诚实返回 None，不能猜。"""
    parsed = ParsedDocument(text="随便什么", layout=None)
    assert locate(parsed, page=1, char_start=None, char_end=None) is None


def test_locate_returns_none_when_page_has_no_lines(tmp_path):
    """页号对不上任何一页版面时，退回文本片段而不是给出空框数组。"""
    parsed = default_parser.parse(_pdf(tmp_path / "c.pdf"), ".pdf")
    loc = locate(parsed, page=99, char_start=0, char_end=5)
    assert loc is not None and loc.kind == "text_range"


def test_locate_sheet_range_highlights_the_covering_rows():
    parsed, rows = _sheet_fixture()
    # 落在前两行数据（r1 / r2）上
    loc = locate(
        parsed, page=None, char_start=rows[0].char_start, char_end=rows[1].char_end
    )

    assert loc is not None and loc.kind == "sheet_range"
    assert loc.sheet == "Sheet1"
    assert loc.header == ("h1", "h2")
    assert loc.highlight == (0, 2)
    start, end = loc.highlight
    assert [r[0] for r in loc.rows[start:end]] == ["r1", "r2"]
    assert [r[0] for r in loc.rows[:start] + loc.rows[end:]] == ["r3"]


def test_normalised_box_round_trips_back_onto_the_target_line(tmp_path):
    """中心承诺的回归：归一化框反算回页面坐标后，必须**盖住目标行、且只盖它**。

    这是整个功能的立身之本 —— 框错一行，用户看到的就是"依据在这里"而其实不在。
    刻度走一遍 归一化 → 反归一化，任何比例写反/写错位都会在这一步暴露。
    """
    parsed = default_parser.parse(_pdf(tmp_path / "round.pdf"), ".pdf")
    page = parsed.layout.pages[0]
    target = page.lines[2]

    loc = locate(parsed, page=1, char_start=target.char_start, char_end=target.char_end)
    assert loc is not None and loc.kind == "page_image"
    assert len(loc.boxes) == 1

    x0, y0, x1, y1 = loc.boxes[0]
    box = (x0 * page.width, y0 * page.height, x1 * page.width, y1 * page.height)
    # 与目标行重合（留 1pt 容差吸收浮点）
    assert abs(box[0] - target.x0) < 1.0
    assert abs(box[1] - target.y0) < 1.0
    assert abs(box[2] - target.x1) < 1.0
    assert abs(box[3] - target.y1) < 1.0

    # 与相邻行**不相交** —— 上下各一行都必须干净
    for neighbour in (page.lines[1], page.lines[3]):
        disjoint = box[3] <= neighbour.y0 or box[1] >= neighbour.y1
        assert disjoint, f"框越界盖到了相邻行 {neighbour.text!r}"


def test_normalised_boxes_are_clamped_into_unit_range():
    # 解析器偶尔给出略微越界的 bbox（旋转页、裁剪框），归一化必须夹住
    lines = (
        LineBox(text="x", x0=-5.0, y0=-5.0, x1=700.0, y1=900.0, char_start=0, char_end=1),
    )
    parsed = ParsedDocument(
        text="x",
        layout=DocumentLayout(
            pages=(PageLines(page=1, width=600.0, height=800.0, lines=lines),)
        ),
    )
    loc = locate(parsed, page=1, char_start=0, char_end=1)
    assert loc is not None and loc.kind == "page_image"
    x0, y0, x1, y1 = loc.boxes[0]
    assert x0 >= 0.0 and x1 <= 1.0 and y0 >= 0.0 and y1 <= 1.0
