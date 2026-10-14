"""版面索引：行框的位置、字符区间，以及"宁可不给也不给错"的丢弃语义。"""
from __future__ import annotations

import pymupdf

from app.rag.layout import DocumentLayout, pdf_lines
from app.rag.parsers import default_parser


def _make_pdf(path, pages: int = 2) -> str:
    """造一份两页 PDF，每页三个文本块、每块五行。"""
    doc = pymupdf.open()
    for pg in range(pages):
        page = doc.new_page()
        for bi, (x, y) in enumerate([(72, 72), (72, 220), (320, 72)]):
            for li in range(5):
                page.insert_text(
                    (x, y + li * 14), f"Blk{bi} L{li} 条款 8.5.1-{pg}-{bi}-{li}", fontsize=10
                )
    doc.save(str(path))
    doc.close()
    return str(path)


def test_pdf_layout_lines_are_inside_page_and_ordered(tmp_path):
    src = _make_pdf(tmp_path / "a.pdf")
    parsed = default_parser.parse(src, ".pdf")

    assert parsed.layout is not None
    assert len(parsed.layout.pages) == 2

    page = parsed.layout.pages[0]
    assert page.page == 1
    assert page.lines, "第一页应当提取到文本行"
    # 字符区间必须单调递增，且真的指向 text 里的那段内容
    prev = -1
    for line in page.lines:
        assert line.char_start >= prev, "行偏移必须单调不减"
        prev = line.char_start
        assert parsed.text[line.char_start : line.char_end] == line.text
        # 框必须落在页内，且是有面积的正矩形
        assert 0 <= line.x0 < line.x1 <= page.width
        assert 0 <= line.y0 < line.y1 <= page.height


def test_pdf_layout_text_is_unchanged_by_layout_extraction(tmp_path):
    """加版面索引不得改变 text —— text 变了就是隐性重索引。"""
    src = _make_pdf(tmp_path / "b.pdf")
    parsed = default_parser.parse(src, ".pdf")
    doc = pymupdf.open(src)
    try:
        expected = "\n\n".join(p.get_text("text").strip() for p in doc if p.get_text("text").strip())
    finally:
        doc.close()
    assert parsed.text == expected


def test_pdf_lines_drops_lines_it_cannot_locate(tmp_path):
    """行文本在页文本里找不到时必须丢弃，而不是错位挂上别人的区间。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "真实存在的行", fontsize=10)
    # 直接把一段不在页文本里的行喂进去
    boxes = pdf_lines(page, "完全不同的页文本", base=0)
    doc.close()
    assert boxes == []


def test_document_layout_defaults_are_empty():
    layout = DocumentLayout()
    assert layout.pages == () and layout.sheets == () and layout.slides == ()
    assert layout.empty is True
