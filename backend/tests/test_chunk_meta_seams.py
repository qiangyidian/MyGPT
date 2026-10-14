"""切块接缝处的字符锚点。

``chunk_meta._offsets`` 靠 ``text.find(chunk)`` 反推每个 chunk 在原文里的位置。
精确切片上这没问题，但**真切块器的输出不是精确切片**：重叠窗口跨过分页接缝时，
原文里的 ``"\\n\\n"`` 会被归一成 ``"\\n"``（或多出/少掉一个换行），于是 find 失败、
``char_start`` 落成 ``None``。

后果不是"高亮不了"这么轻：引用上的「第 N 页」、`heading`、以及本轮的原文定位
全部依赖它，所以接缝上的 chunk 会**同时**丢掉这三样东西。既有测试喂的都是精确
切片，因此这个缺陷一直没被看见 —— 本文件专门盯住它。
"""
from __future__ import annotations

from pathlib import Path

import pymupdf

from app.rag.base import ParsedDocument
from app.rag.chunk_meta import annotate
from app.rag.parsers import default_parser
from app.rag.splitter import RecursiveTextSplitter


def _parsed(text: str) -> ParsedDocument:
    return ParsedDocument(text=text, pages=[p for p in text.split("\n\n") if p])


def _multi_page_pdf(path: Path, pages: int = 3, lines: int = 30) -> str:
    doc = pymupdf.open()
    for pg in range(pages):
        page = doc.new_page()
        for i in range(lines):
            page.insert_text(
                (72, 72 + i * 16), f"P{pg} line{i} 条款内容 ABC-{pg}-{i}", fontsize=10
            )
    doc.save(str(path))
    doc.close()
    return str(path)


def test_exact_slice_chunks_keep_their_exact_span():
    """精确切片走的是快路径 —— 接缝容错不得改变它们的区间语义。"""
    text = "第一段内容\n\n第二段内容\n\n第三段内容"
    chunks = ["第一段内容", "第二段内容", "第三段内容"]

    spans = annotate(chunks, _parsed(text))

    for chunk, span in zip(chunks, spans, strict=True):
        assert span.char_start is not None
        assert text[span.char_start : span.char_end] == chunk


def test_chunk_whose_seam_whitespace_was_normalised_is_still_located():
    """接缝处 ``"\\n\\n"`` 被归一成 ``"\\n"`` 的 chunk 必须仍被定位到。"""
    text = "甲组第一行\n甲组第二行\n\n乙组第一行\n乙组第二行"
    # 切块器在跨段接缝处把空行压掉了 —— 这不是原文的连续子串
    chunk = "甲组第二行\n乙组第一行"

    span = annotate([chunk], _parsed(text))[0]

    assert span.char_start is not None, "接缝上的 chunk 不该丢掉锚点"
    covered = text[span.char_start : span.char_end]
    assert covered.startswith("甲组第二行")
    assert covered.endswith("乙组第一行")


def test_real_splitter_on_a_multipage_pdf_anchors_every_chunk(tmp_path):
    """端到端：真切块器 + 真解析器，每个 chunk 都要拿到锚点与页号。

    这是"老文档自动可用"的前提 —— 锚点来自入库时的切块步骤，一旦这里大面积
    落空，原文定位就大面积不可用。
    """
    parsed = default_parser.parse(_multi_page_pdf(tmp_path / "a.pdf"), ".pdf")
    chunks = RecursiveTextSplitter().split(parsed.text)
    spans = annotate(chunks, parsed)

    assert len(chunks) >= 3
    missing = [i for i, s in enumerate(spans) if s.char_start is None]
    assert not missing, f"以下 chunk 丢掉了字符锚点：{missing}"
    assert all(s.page is not None for s in spans), "每个 chunk 都该有页号"
    starts = [s.char_start for s in spans]
    assert starts == sorted(starts), "chunk 按文档顺序产出，锚点必须单调不减"


def test_text_that_is_genuinely_absent_still_yields_none():
    """真正的"找不到"仍然是 None —— 容错不能退化成硬猜。"""
    span = annotate(["这段文字根本不在原文里 at-all-42"], _parsed("完全无关的一小段"))[0]
    assert span.char_start is None
