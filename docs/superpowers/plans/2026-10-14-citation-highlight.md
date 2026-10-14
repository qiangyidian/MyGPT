# 引用原文定位与高亮 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 点击引用来源后，能在右侧抽屉里看到该 chunk 在原件中的确切位置——PDF 到行级高亮框，表格到行级，幻灯片到形状级，其余格式到文本片段级。

**Architecture:** 坐标不落库。解析器额外产出一份「版面索引」（每页/每行/每形状都带它在 `ParsedDocument.text` 里的字符区间），入库时一切照旧；点击时用 chunk 已有的 `char_start/char_end/page` 锚点重解析原件、现算位置。页面图与高亮框解耦：图是干净的整页 PNG（可长缓存），框是按 chunk 返回的归一化比例，前端用 CSS 百分比叠加。

**Tech Stack:** 后端 Python 3.13 / FastAPI / PyMuPDF 1.28 / pandas / python-pptx；前端 Next.js / TypeScript / vitest。

**Spec:** `docs/superpowers/specs/2026-10-14-citation-highlight-design.md`

## Global Constraints

- **零迁移、零重 embedding**：不加数据库列，不改 Qdrant payload，不跑回填。`Char` 锚点 `char_start/char_end/page` 已在 `document_chunks.metadata_` 里（自迁移 0015）。
- **`ParsedDocument.text` 对既有格式逐字节不变**。text 是切块与 embedding 的输入，改了就是隐性重索引。任何解析器改动都必须带这条回归断言。
- **坐标一律左上原点、y 向下、单位 pt**（PyMuPDF 原生系）。若接入左手原点来源（如 Docling 的 BOTTOMLEFT），必须在写进 layout 之前翻好，不允许两套坐标系流进同一个结构。
- **高亮框一行一个，绝不把多行并成一个矩形**——并框等于把相邻 chunk 的内容也框进去。
- **注释与用户文案用中文**；注释里允许全角标点（`pyproject.toml` 已关 `RUF001/RUF002/RUF003`）。
- **只跑 `ruff check`，不跑 `ruff format`**（仓库有历史格式债，全量 reformat 会冲垮 diff）。
- 本地检查命令见仓库 `CLAUDE.md` 第 4 节；两个网络依赖用例与一个已知死锁用例按该节 deselect。
- **不做**：LibreOffice、pdf.js、附件高亮、DOCX 原页高亮。

## 文件结构

| 文件 | 职责 |
|---|---|
| `backend/app/rag/layout.py`（建） | 版面索引的数据结构 + 页基准偏移记账 + PDF 行提取。只描述数据 |
| `backend/app/rag/locator.py`（建） | `locate(parsed, page, char_start, char_end)` → 四种 Locator。只做定位，不碰 HTTP/DB |
| `backend/app/rag/base.py`（改） | `ParsedDocument` 加 `layout` 字段 |
| `backend/app/rag/parsers.py`（改） | `_parse_pdf` / `_parse_table` / `_parse_pptx` 产出 layout |
| `backend/app/schemas/document.py`（改） | `LocatorOut` 响应模型 |
| `backend/app/api/documents.py`（改） | 两个只读端点 |
| `backend/tests/test_layout.py`（建） | 版面索引与 PDF 行提取 |
| `backend/tests/test_locator.py`（建） | 四种 kind 的挑选与降级 |
| `backend/tests/test_document_locator_api.py`（建） | 端点鉴权、错误码、ETag |
| `frontend/src/lib/locator.ts`（建） | Locator 类型 + 归一化换算纯函数 |
| `frontend/src/lib/api.ts`（改） | 两个客户端方法 |
| `frontend/src/components/context/highlight-drawer.tsx`（建） | 抽屉壳 + kind 分派 |
| `frontend/src/components/context/locators/`（建） | 四个渲染器 |
| `frontend/src/components/context/sources-tab.tsx`（改） | 「查看原文位置」按钮 |

---

### Task 1: 版面索引与 PDF 行提取

**Files:**
- Create: `backend/app/rag/layout.py`
- Modify: `backend/app/rag/base.py`（`ParsedDocument` 加 `layout` 字段）
- Modify: `backend/app/rag/parsers.py:114-190`（`_parse_pdf`）
- Test: `backend/tests/test_layout.py`

**Interfaces:**
- Consumes: 无（本任务是最底层）
- Produces:
  - `layout.LineBox(text: str, x0/y0/x1/y1: float, char_start: int, char_end: int)`
  - `layout.PageLines(page: int, width: float, height: float, lines: tuple[LineBox, ...])`
  - `layout.SheetRow(row: int, char_start: int, char_end: int)`
  - `layout.SheetLayout(name: str, rows: tuple[SheetRow, ...])`
  - `layout.ShapeBox(x, y, w, h: float, text: str, char_start: int, char_end: int)`
  - `layout.SlideLayout(slide_no: int, width: float, height: float, shapes: tuple[ShapeBox, ...])`
  - `layout.DocumentLayout(pages=(), sheets=(), slides=())`
  - `layout.pdf_lines(page_obj, page_text: str, base: int) -> list[LineBox]`
  - `base.ParsedDocument.layout: DocumentLayout | None`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_layout.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_layout.py -q --tb=short`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.rag.layout'`

- [ ] **Step 3: 写 `layout.py`**

创建 `backend/app/rag/layout.py`：

```python
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
```

- [ ] **Step 4: 给 `ParsedDocument` 加字段**

修改 `backend/app/rag/base.py`：在 `ParsedDocument` 的 `metadata` 字段之后加

```python
    # 版面索引：能做坐标的解析器才填（PDF / 表格 / 幻灯片）。``None`` 表示这个
    # 格式没有坐标可言 —— 引用定位会退到"原文文本片段"那一层，而不是报错。
    layout: Any = None
```

（`base.py` 顶部已 `from typing import Any`，无需新增 import。）

- [ ] **Step 5: `_parse_pdf` 产出布局**

修改 `backend/app/rag/parsers.py`。顶部 import 区加：

```python
from app.rag.layout import DocumentLayout, LineBox, PageLines, pdf_lines
```

`_parse_pdf` 里，第一遍循环改为同时收集行（页局部偏移）：

```python
    doc = pymupdf.open(path)
    # 页局部行框（偏移相对该页文本）+ 页尺寸；OCR 之后才知道哪些页要作废，
    # 所以先收着，join 之后再换算成全文偏移。
    local_lines: dict[int, list[LineBox]] = {}
    page_sizes: dict[int, tuple[float, float]] = {}
    try:
        for number, page in enumerate(doc, start=1):
            text = (page.get_text("text") or "").strip()
            local_lines[number] = pdf_lines(page, text, base=0)
            page_sizes[number] = (float(page.rect.width), float(page.rect.height))
            # Table extraction (PyMuPDF >= 1.23). Best-effort; some pages raise.
            try:
                finder = page.find_tables()
                for tbl in getattr(finder, "tables", []) or []:
                    rows = tbl.extract()
                    rendered = _table_to_text(rows)
                    if rendered.strip():
                        tables.append(rendered)
            except Exception:
                pass
            pages.append(text)
    finally:
        doc.close()
```

注意：原来用 `for page in doc:`（0-based 隐式），现在要页号，改成 `enumerate(..., start=1)`；`pages.append(text)` 里的顺序不变。

函数末尾 `return ParsedDocument(...)` 之前，构造 layout：

```python
    # OCR 替换过文本的页没有版面坐标（文字来自 Tesseract），必须作废，
    # 否则行偏移会指向一段已经不是该页内容的文本。
    ocr_pages = set((ocr_by_page or {}).keys())
    bases = page_bases(pages)
    rendered_pages: list[PageLines] = []
    for number, lines in local_lines.items():
        if number in ocr_pages:
            continue
        base = bases.get(number)
        width_height = page_sizes.get(number)
        if base is None or width_height is None or not lines:
            continue
        rendered_pages.append(
            PageLines(
                page=number,
                width=width_height[0],
                height=width_height[1],
                lines=tuple(
                    LineBox(
                        text=ln.text,
                        x0=ln.x0,
                        y0=ln.y0,
                        x1=ln.x1,
                        y1=ln.y1,
                        char_start=base + ln.char_start,
                        char_end=base + ln.char_end,
                    )
                    for ln in lines
                ),
            )
        )
    layout = DocumentLayout(pages=tuple(rendered_pages)) if rendered_pages else None
```

并把 `layout=layout` 加进 `ParsedDocument(...)` 构造。

`ocr_by_page` 在 `if settings.OCR_SCANNED_PDF:` 分支里才赋值，所以函数开头要初始化 `ocr_by_page: dict[int, str] = {}`，把分支里的 `ocr_by_page = {...}` 改成原地赋值（不要重新绑定新变量）。**注意**：`local_lines` 的偏移是相对 `"".join(pages)` 用的**未 strip 的页文本**吗？不是——`pdf_lines(page, text, 0)` 传的 `text` 是 `.strip()` 过的，而 `pages[i]` 存的也是同一个 strip 过的字符串，`page_bases` 用的是 `pages` 列表，两者一致。

- [ ] **Step 6: 跑测试确认通过**

Run: `cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_layout.py -q --tb=short`
Expected: PASS（4 项）

- [ ] **Step 7: 跑既有 RAG 回归**

Run: `cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_rag_claims.py tests/test_chunk_meta.py -q --tb=short`
Expected: PASS。若 `test_chunk_meta.py` 不存在，跳过该文件名，改用 `ls tests | grep -i chunk` 找实际文件名。

- [ ] **Step 8: lint**

Run: `cd backend && ./.venv/Scripts/python.exe -m ruff check app tests`
Expected: 无输出（通过）

- [ ] **Step 9: 提交**

```bash
cd backend && git add app/rag/layout.py app/rag/base.py app/rag/parsers.py tests/test_layout.py
git commit -m "feat(rag): PDF 版面索引（行框 + 字符区间，找不到的行丢弃）"
```

---

### Task 2: 定位器

**Files:**
- Create: `backend/app/rag/locator.py`
- Test: `backend/tests/test_locator.py`

**Interfaces:**
- Consumes: Task 1 的 `layout.DocumentLayout` / `PageLines` / `LineBox` / `SheetLayout` / `SlideLayout`
- Produces:
  - `locator.PageImageLocator(kind="page_image", page: int, total_pages: int, boxes: tuple[tuple[float, float, float, float], ...])`
  - `locator.SheetRangeLocator(kind="sheet_range", sheet: str, header: tuple[str, ...], rows: tuple[tuple[str, ...], ...], highlight: tuple[int, int])`
  - `locator.SlideLocator(kind="slide", slide_no: int, width: float, height: float, shapes: tuple[ShapeBox, ...])`，其中 `ShapeBox` 复用 `layout` 的加一个 `hit: bool`——本计划在 `locator.py` 里另定义 `SlideShape(rect: tuple[float, float, float, float], text: str, hit: bool)`
  - `locator.TextRangeLocator(kind="text_range", text: str, start: int, end: int)`
  - `locator.locate(parsed, *, page, char_start, char_end, text_context=800) -> Locator | None`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_locator.py`：

```python
"""定位器：四种 kind 的挑选顺序、归一化，以及一切"定位不了"都必须return None。"""
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


def test_locate_falls_back_to_text_range_without_layout(tmp_path):
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
    text = "h1 | h2\nr1 | a\nr2 | b\nr3 | c"
    rows = []
    cursor = 0
    for i, line in enumerate(text.split("\n")):
        rows.append(SheetRow(row=i, char_start=cursor, char_end=cursor + len(line)))
        cursor += len(line) + 1
    parsed = ParsedDocument(
        text=text, tables=[text], layout=DocumentLayout(sheets=(SheetLayout(name="Sheet1", rows=tuple(rows)),))
    )
    # 落在第二、三行（r1 / r2）上
    loc = locate(parsed, page=None, char_start=rows[1].char_start, char_end=rows[2].char_end)

    assert loc is not None and loc.kind == "sheet_range"
    assert loc.sheet == "Sheet1"
    assert loc.header == ("h1", "h2")
    assert loc.highlight == (1, 3), "高亮行区间是 [起, 止)"


def test_normalised_boxes_are_clamped_into_unit_range():
    # 解析器偶尔给出略微越界的 bbox（旋转页、裁剪框），归一化必须夹住
    lines = (LineBox(text="x", x0=-5.0, y0=-5.0, x1=700.0, y1=900.0, char_start=0, char_end=1),)
    parsed = ParsedDocument(
        text="x",
        layout=DocumentLayout(pages=(PageLines(page=1, width=600.0, height=800.0, lines=lines),)),
    )
    loc = locate(parsed, page=1, char_start=0, char_end=1)
    assert loc is not None and loc.kind == "page_image"
    x0, y0, x1, y1 = loc.boxes[0]
    assert 0.0 <= x0 and x1 <= 1.0 and 0.0 <= y0 and y1 <= 1.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_locator.py -q --tb=short`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.rag.locator'`

- [ ] **Step 3: 写 `locator.py`**

创建 `backend/app/rag/locator.py`：

```python
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
from typing import Any, Union

from app.rag.base import ParsedDocument

#: 文本兜底层给出的窗口在命中区间前后各留多少字。太小则用户看不出上下文，
#: 太大则把一个抽屉塞满 —— 800 字约等于两屏。
DEFAULT_TEXT_CONTEXT = 800


@dataclass(frozen=True)
class PageImageLocator:
    kind: str
    page: int
    total_pages: int
    #: 归一化框（相对页面宽高的比例），一行一个，绝不合并。
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


Locator = Union[PageImageLocator, SheetRangeLocator, SlideLocator, TextRangeLocator]


def _clamp(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def _normalise(line: Any, page: Any) -> tuple[float, float, float, float]:
    """行框 → 归一化比例。除零与越界都必须挡住：比例越界前端会画出跑到页外的框。"""
    if page.width <= 0 or page.height <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        _clamp(line.x0 / page.width),
        _clamp(line.y0 / page.height),
        _clamp(line.x1 / page.width),
        _clamp(line.y1 / page.height),
    )


def _locate_page(layout: Any, page: int | None, start: int, end: int) -> PageImageLocator | None:
    if page is None:
        return None
    for page_lines in layout.pages:
        if page_lines.page != page:
            continue
        hits = [ln for ln in page_lines.lines if ln.char_start < end and ln.char_end > start]
        if not hits:
            return None
        return PageImageLocator(
            kind="page_image",
            page=page,
            total_pages=len(layout.pages),
            boxes=tuple(_normalise(ln, page_lines) for ln in hits),
        )
    return None


def _row_cells(line: str, columns: int) -> tuple[str, ...]:
    cells = [c.strip() for c in line.split(" | ")]
    return tuple((cells + [""] * columns)[:columns])


def _locate_sheet(parsed: ParsedDocument, layout: Any, start: int, end: int) -> SheetRangeLocator | None:
    for index, sheet in enumerate(layout.sheets):
        rows = sheet.rows
        if not rows:
            continue
        if not (rows[0].char_start <= start < rows[-1].char_end):
            continue
        hits = [r for r in rows if r.char_start < end and r.char_end > start]
        if not hits:
            return None
        first, last = hits[0].row, hits[-1].row + 1  # [起, 止)
        blocks = parsed.tables or parsed.pages or []
        if index >= len(blocks):
            return None
        lines = blocks[index].splitlines()
        if not lines:
            return None
        columns = len(lines[0].split(" | "))
        header = _row_cells(lines[0], columns)
        body = tuple(_row_cells(ln, columns) for ln in lines[1:])
        # highlight 是相对 body（表头不算行）的区间
        return SheetRangeLocator(
            kind="sheet_range",
            sheet=sheet.name,
            header=header,
            rows=body,
            highlight=(max(0, first - 1), max(0, last - 1)),
        )
    return None


def _locate_slide(layout: Any, start: int, end: int) -> SlideLocator | None:
    for slide in layout.slides:
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
        hit = _locate_page(layout, page, char_start, char_end)
        if hit is not None:
            return hit
        hit = _locate_sheet(parsed, layout, char_start, char_end)
        if hit is not None:
            return hit
        hit = _locate_slide(layout, char_start, char_end)
        if hit is not None:
            return hit

    low = max(0, char_start - text_context)
    high = min(len(text), char_end + text_context)
    if high <= low:
        return None
    return TextRangeLocator(kind="text_range", text=text[low:high], start=char_start - low, end=char_end - low)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_locator.py -q --tb=short`
Expected: PASS（8 项）

- [ ] **Step 5: lint**

Run: `cd backend && ./.venv/Scripts/python.exe -m ruff check app tests`
Expected: 通过

- [ ] **Step 6: 提交**

```bash
cd backend && git add app/rag/locator.py tests/test_locator.py
git commit -m "feat(rag): 定位器（页内行框 / 表格行 / 幻灯片形状 / 文本窗口，四级降级）"
```

---

### Task 3: 两个只读端点

**Files:**
- Modify: `backend/app/schemas/document.py`（加 `LocatorOut`）
- Modify: `backend/app/api/documents.py`（加两个路由）
- Test: `backend/tests/test_document_locator_api.py`

**Interfaces:**
- Consumes: Task 2 的 `locate()` 与四种 Locator；`documents._load_owned_doc`；`parsers.default_parser`
- Produces:
  - `GET /api/documents/{document_id}/chunks/{chunk_id}/locator` → `LocatorOut`
  - `GET /api/documents/{document_id}/pages/{page_no}/image?dpi=150` → `image/png`（带 ETag）

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_document_locator_api.py`。沿用仓库既有的 API 测试夹具（`conftest.py` 提供 `client` / `auth_headers` / `db_session`）——**先打开 `backend/tests/test_attachments.py` 或 `test_documents.py` 抄一份登录与建档夹具**，不要自己发明。

```python
"""定位端点的鉴权、错误码与缓存语义。

本文件只测"端点这一层"的语义：谁不能看、看不了时给什么码、缓存对不对。
定位算法本身在 test_locator.py 里测。
"""
from __future__ import annotations

async def test_locator_404_for_someone_elses_document(client, other_user_headers, indexed_pdf_doc):
    """别人的文档一律 404，不区分"不存在"与"不是你的"。"""
    doc_id, chunk_id = indexed_pdf_doc
    resp = await client.get(
        f"/api/documents/{doc_id}/chunks/{chunk_id}/locator", headers=other_user_headers
    )
    assert resp.status_code == 404


async def test_locator_reports_no_locator_for_chunk_without_char_anchors(client, auth_headers, indexed_pdf_doc):
    """没有 char 偏移的 chunk 必须给机器可读的 no_locator，而不是空白 200。"""
    doc_id, chunk_id = indexed_pdf_doc
    # 夹具把该 chunk 的 char_start/char_end 抹掉
    resp = await client.get(
        f"/api/documents/{doc_id}/chunks/{chunk_id}/locator", headers=auth_headers
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "no_locator"


async def test_page_image_sets_etag_and_answers_304(client, auth_headers, indexed_pdf_doc):
    doc_id, _ = indexed_pdf_doc
    first = await client.get(
        f"/api/documents/{doc_id}/pages/1/image", headers=auth_headers
    )
    assert first.status_code == 200
    assert first.headers["content-type"] == "image/png"
    etag = first.headers["etag"]
    assert etag

    second = await client.get(
        f"/api/documents/{doc_id}/pages/1/image",
        headers={**auth_headers, "If-None-Match": etag},
    )
    assert second.status_code == 304


async def test_page_image_rejects_out_of_range_page_and_dpi(client, auth_headers, indexed_pdf_doc):
    doc_id, _ = indexed_pdf_doc
    assert (
        await client.get(f"/api/documents/{doc_id}/pages/9999/image", headers=auth_headers)
    ).status_code == 404
    assert (
        await client.get(
            f"/api/documents/{doc_id}/pages/1/image?dpi=9999", headers=auth_headers
        )
    ).status_code == 422
```

**夹具说明**：`indexed_pdf_doc` 需要在本文件里实现——用 Task 1 的 `_make_pdf` 生成一份 PDF 落到 `tmp_path`，建知识库、建 `Document` 行（`file_path` 指向该文件、`file_type=".pdf"`、`status="indexed"`），再插一条 `DocumentChunk`（`metadata_` 带 `page/char_start/char_end`）。照着 `backend/tests/test_documents.py` 里已有的建档夹具写。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_document_locator_api.py -q --tb=short`
Expected: FAIL — 404（路由不存在）

- [ ] **Step 3: 加响应模型**

在 `backend/app/schemas/document.py` 末尾加：

```python
class LocatorOut(BaseModel):
    """一个 chunk 在原件里的位置。

    扁平的可选字段而不是判别式联合：前端按 ``kind`` 取用，多出来的字段一律
    忽略。这与 ``Citation`` 的既有风格一致，也让加新 kind 不需要改老客户端。
    """

    kind: str                       # page_image | sheet_range | slide | text_range
    # page_image
    page: int | None = None
    total_pages: int | None = None
    image_url: str | None = None
    boxes: list[list[float]] = []   # 归一化 [x0, y0, x1, y1]，一行一个
    # sheet_range
    sheet: str | None = None
    header: list[str] = []
    rows: list[list[str]] = []
    highlight: list[int] | None = None   # [起, 止)
    # slide
    slide_no: int | None = None
    width: float | None = None
    height: float | None = None
    shapes: list[dict[str, Any]] = []
    # text_range
    text: str | None = None
    start: int | None = None
    end: int | None = None
```

确认该文件顶部已有 `from typing import Any`；没有就加。

- [ ] **Step 4: 加端点**

`backend/app/api/documents.py` 顶部 import 区补：

```python
import hashlib

from fastapi import Request, Response
```

（`hashlib` 若已存在则不必重复。）再加两个路由，放在 `download_document` 之后：

```python
#: 单页渲染的 dpi 上下限。上限存在的理由是内存：A4 在 300dpi 下已经是约
#: 2500×3500 的位图，再往上一次请求就能吃掉几百 MB。
_MIN_DPI, _MAX_DPI = 72, 300
#: 单页面积上限（pt²）。超出的页（工程图、超长海报）不渲染，避免拖垮进程。
_MAX_PAGE_AREA_PT = 2_000_000.0


def _render_page_png(path: str, page_no: int, dpi: int) -> bytes:
    """渲染一页为 PNG。**不带任何高亮** —— 框由 locator 单独给。"""
    import pymupdf

    doc = pymupdf.open(path)
    try:
        if page_no < 1 or page_no > doc.page_count:
            raise IndexError(page_no)
        page = doc[page_no - 1]
        rect = page.rect
        if rect.width * rect.height > _MAX_PAGE_AREA_PT:
            raise ValueError("page too large to render")
        zoom = dpi / 72.0
        return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")
    finally:
        doc.close()


@router.get("/documents/{document_id}/pages/{page_no}/image")
async def document_page_image(
    document_id: uuid.UUID,
    page_no: int,
    request: Request,
    dpi: int = Query(default=150, ge=_MIN_DPI, le=_MAX_DPI),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """渲染文档的某一页，供引用定位的高亮底图使用。

    图**不包含高亮框**：同一个 chunk 被反复点开、同一页被多个引用命中时，
    图应当只渲染一次、只缓存一份。ETag 由（原件指纹, 页号, dpi）决定，
    所以它在文档被重新上传之前永远有效。
    """
    doc = await _load_owned_doc(db, document_id, user)
    if not doc.file_path or not os.path.exists(doc.file_path):
        raise AppException(
            status_code=NOT_FOUND,
            code="storage_gone",
            message="文档原件已不存在，无法渲染原页",
        )
    fingerprint = doc.content_sha256 or doc.filename or ""
    etag = '"%s"' % hashlib.sha256(
        f"{fingerprint}:{page_no}:{dpi}".encode("utf-8")
    ).hexdigest()
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})

    try:
        payload = await asyncio.to_thread(_render_page_png, doc.file_path, page_no, dpi)
    except IndexError:
        raise HTTPException(NOT_FOUND, "页码超出范围")
    except Exception as exc:
        raise HTTPException(500, f"渲染失败: {exc}") from exc

    return Response(
        content=payload,
        media_type="image/png",
        headers={"ETag": etag, "Cache-Control": "private, max-age=86400"},
    )


def _locator_to_out(loc: Any, document_id: uuid.UUID) -> LocatorOut:
    if loc.kind == "page_image":
        return LocatorOut(
            kind=loc.kind,
            page=loc.page,
            total_pages=loc.total_pages,
            image_url=f"/api/documents/{document_id}/pages/{loc.page}/image",
            boxes=[[float(v) for v in box] for box in loc.boxes],
        )
    if loc.kind == "sheet_range":
        return LocatorOut(
            kind=loc.kind,
            sheet=loc.sheet,
            header=list(loc.header),
            rows=[list(r) for r in loc.rows],
            highlight=[loc.highlight[0], loc.highlight[1]],
        )
    if loc.kind == "slide":
        return LocatorOut(
            kind=loc.kind,
            slide_no=loc.slide_no,
            width=loc.width,
            height=loc.height,
            shapes=[
                {"rect": list(s.rect), "text": s.text, "hit": s.hit} for s in loc.shapes
            ],
        )
    return LocatorOut(kind=loc.kind, text=loc.text, start=loc.start, end=loc.end)


@router.get(
    "/documents/{document_id}/chunks/{chunk_id}/locator",
    response_model=LocatorOut,
)
async def locate_document_chunk(
    document_id: uuid.UUID,
    chunk_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LocatorOut:
    """一个 chunk 在原件里的位置。

    坐标是**请求时现算**的：用 chunk 库里已有的 char 锚点，在刚刚重解析出的
    原件上定位。这样定位结果永远与当前解析器自洽，而坐标不必随解析器版本
    一起落库、也就不会静默过期。代价是一次重解析 —— 与
    ``/documents/{id}/preview`` 同一个取舍，那条路径早就在这么做了。
    """
    doc = await _load_owned_doc(db, document_id, user)
    chunk = await db.get(DocumentChunk, chunk_id)
    if chunk is None or chunk.document_id != doc.id:
        raise HTTPException(NOT_FOUND, "片段不存在")
    if not doc.file_path or not os.path.exists(doc.file_path):
        raise AppException(
            status_code=NOT_FOUND,
            code="storage_gone",
            message="文档原件已不存在，无法定位原文位置",
        )
    if doc.status in ("pending", "parsing", "chunking", "embedding"):
        raise HTTPException(status.HTTP_409_CONFLICT, "文档正在解析中，请稍后再试")

    from app.rag.locator import locate
    from app.rag.parsers import default_parser

    try:
        parsed = await asyncio.to_thread(default_parser.parse, doc.file_path, doc.file_type)
    except Exception as exc:
        raise HTTPException(500, f"解析失败: {exc}") from exc

    meta = chunk.metadata_ or {}
    loc = locate(
        parsed,
        page=meta.get("page"),
        char_start=meta.get("char_start"),
        char_end=meta.get("char_end"),
    )
    if loc is None:
        raise AppException(
            status_code=NOT_FOUND,
            code="no_locator",
            message="该文档无法定位到原文位置（原件格式不含坐标，或这个片段入库时没有记录位置）",
        )
    return _locator_to_out(loc, doc.id)
```

import 区补 `LocatorOut`、`DocumentChunk`、`Query`、`AppException`（若缺）。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_document_locator_api.py -q --tb=short`
Expected: PASS

- [ ] **Step 6: 跑既有文档端点回归**

Run: `cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_documents.py -q --tb=short`
Expected: PASS

- [ ] **Step 7: lint 并提交**

```bash
cd backend && ./.venv/Scripts/python.exe -m ruff check app tests
git add app/schemas/document.py app/api/documents.py tests/test_document_locator_api.py
git commit -m "feat(api): chunk 定位端点 + 原页渲染端点（ETag 长缓存，dpi 限幅）"
```

---

### Task 4: 前端 locator 类型与归一化换算

**Files:**
- Create: `frontend/src/lib/locator.ts`
- Modify: `frontend/src/lib/api.ts`
- Test: `frontend/src/lib/__tests__/locator.test.ts`

**Interfaces:**
- Consumes: 后端 `LocatorOut` 的 JSON 形状
- Produces:
  - `type Locator = PageImageLocator | SheetRangeLocator | SlideLocator | TextRangeLocator`
  - `toPercentBox(box: number[]): { left: string; top: string; width: string; height: string }`
  - `highlightedRows(loc: SheetRangeLocator): number[]`
  - `splitTextRange(loc: TextRangeLocator): [string, string, string]`
  - `api.locateChunk(documentId: string, chunkId: string): Promise<Locator>`
  - `api.documentPageImage(documentId: string, page: number, dpi?: number): Promise<Blob>`

- [ ] **Step 1: 写失败的测试**

创建 `frontend/src/lib/__tests__/locator.test.ts`：

```ts
import { describe, expect, it } from "vitest";

import {
  highlightedRows,
  splitTextRange,
  toPercentBox,
  type SheetRangeLocator,
  type TextRangeLocator,
} from "@/lib/locator";

describe("toPercentBox", () => {
  it("把归一化比例换算成 CSS 百分比", () => {
    expect(toPercentBox([0.25, 0.5, 0.75, 0.6])).toEqual({
      left: "25%",
      top: "50%",
      width: "50%",
      height: "10%",
    });
  });

  it("夹住越界比例 —— 越界会画出跑到页外的框", () => {
    expect(toPercentBox([-0.2, -1, 1.4, 2])).toEqual({
      left: "0%",
      top: "0%",
      width: "100%",
      height: "100%",
    });
  });
});

describe("highlightedRows", () => {
  it("按 [起, 止) 展开行号", () => {
    const loc = { kind: "sheet_range", highlight: [1, 4] } as SheetRangeLocator;
    expect(highlightedRows(loc)).toEqual([1, 2, 3]);
  });

  it("highlight 缺失时返回空数组而不是崩", () => {
    const loc = { kind: "sheet_range", highlight: null } as unknown as SheetRangeLocator;
    expect(highlightedRows(loc)).toEqual([]);
  });
});

describe("splitTextRange", () => {
  it("切成 前 / 命中 / 后 三段，三段拼回去等于原文", () => {
    const loc: TextRangeLocator = {
      kind: "text_range",
      text: "前面的话命中内容后面的话",
      start: 4,
      end: 8,
    };
    const [before, hit, after] = splitTextRange(loc);
    expect(hit).toBe("命中内容");
    expect(before + hit + after).toBe(loc.text);
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/lib/__tests__/locator.test.ts`
Expected: FAIL — 无法解析 `@/lib/locator`

- [ ] **Step 3: 写 `locator.ts`**

创建 `frontend/src/lib/locator.ts`：

```ts
/**
 * 引用原文定位：后端给的 Locator 形状，以及把它渲染出来所需的纯换算。
 *
 * 高亮框是**归一化比例**（相对页面宽高的 0~1 值），不是像素。这样图与框解耦：
 * 同一页被多个引用命中时图只渲染一次，抽屉缩放时框自动跟随，也不需要任何
 * "坐标系翻转" —— 比例与原点在哪没关系。
 */

export interface PageImageLocator {
  kind: "page_image";
  page: number;
  total_pages: number;
  image_url: string;
  /** 归一化 [x0, y0, x1, y1]，一行一个，绝不合并。 */
  boxes: number[][];
}

export interface SheetRangeLocator {
  kind: "sheet_range";
  sheet: string;
  header: string[];
  rows: string[][];
  /** [起, 止)，相对 rows（表头不算行）。 */
  highlight: [number, number] | null;
}

export interface SlideShape {
  rect: [number, number, number, number];
  text: string;
  hit: boolean;
}

export interface SlideLocator {
  kind: "slide";
  slide_no: number;
  width: number;
  height: number;
  shapes: SlideShape[];
}

export interface TextRangeLocator {
  kind: "text_range";
  text: string;
  start: number;
  end: number;
}

export type Locator =
  | PageImageLocator
  | SheetRangeLocator
  | SlideLocator
  | TextRangeLocator;

function clamp01(value: number): number {
  return value < 0 ? 0 : value > 1 ? 1 : value;
}

/** 归一化框 → CSS 百分比。越界值必须夹住，否则框会画到页面外。 */
export function toPercentBox(box: number[]): {
  left: string;
  top: string;
  width: string;
  height: string;
} {
  const [x0, y0, x1, y1] = box.map(clamp01);
  return {
    left: `${x0 * 100}%`,
    top: `${y0 * 100}%`,
    width: `${Math.max(0, x1 - x0) * 100}%`,
    height: `${Math.max(0, y1 - y0) * 100}%`,
  };
}

/** 高亮行号列表。`highlight` 缺失或非法时给空数组 —— 渲染层不该为此崩。 */
export function highlightedRows(loc: SheetRangeLocator): number[] {
  const range = loc.highlight;
  if (!Array.isArray(range) || range.length !== 2) return [];
  const [start, end] = range;
  const out: number[] = [];
  for (let i = Math.max(0, start); i < end && i < loc.rows.length; i += 1) {
    out.push(i);
  }
  return out;
}

/**
 * 文本窗口切成 前 / 命中 / 后 三段。
 * 后端给的 start/end 是相对窗口的偏移；越界时按窗口长度夹住，保证三段拼回去
 * 仍等于原文（拼接丢失就等于悄悄改了用户的原文）。
 */
export function splitTextRange(loc: TextRangeLocator): [string, string, string] {
  const start = Math.max(0, Math.min(loc.start, loc.text.length));
  const end = Math.max(start, Math.min(loc.end, loc.text.length));
  return [loc.text.slice(0, start), loc.text.slice(start, end), loc.text.slice(end)];
}
```

- [ ] **Step 4: 加 API 客户端方法**

在 `frontend/src/lib/api.ts` 的 `previewDocument` 附近加：

```ts
  /** chunk 在原件里的位置。404 code=no_locator 表示这个片段无法定位。 */
  locateChunk: (documentId: string, chunkId: string) =>
    request<Locator>(
      "GET",
      `/api/documents/${documentId}/chunks/${chunkId}/locator`
    ),
  /**
   * 渲染文档的某一页（干净整页图，不含高亮框）。
   * 走 blob 而不是 `<img src>`：端点要 Bearer token，而 img 标签带不上
   * Authorization 头 —— 直接写 src 会 401。
   */
  documentPageImage: async (
    documentId: string,
    page: number,
    dpi = 150
  ): Promise<Blob> => {
    const res = await request<Response>(
      "GET",
      `/api/documents/${documentId}/pages/${page}/image?dpi=${dpi}`,
      undefined,
      { raw: true }
    );
    return res.blob();
  },
```

并在文件顶部加 `import type { Locator } from "@/lib/locator";`

- [ ] **Step 5: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/lib/__tests__/locator.test.ts`
Expected: PASS（6 项）

- [ ] **Step 6: typecheck 并提交**

```bash
cd frontend && npm run typecheck
git add src/lib/locator.ts src/lib/api.ts src/lib/__tests__/locator.test.ts
git commit -m "feat(web): Locator 类型与归一化换算（比例制，天然免疫坐标系翻转）"
```

---

### Task 5: 抽屉 + 页内高亮渲染器 + Sources 入口

**Files:**
- Create: `frontend/src/components/context/highlight-drawer.tsx`
- Create: `frontend/src/components/context/locators/page-image-locator.tsx`
- Create: `frontend/src/stores/highlight-drawer-store.ts`
- Modify: `frontend/src/components/context/sources-tab.tsx`
- Modify: `frontend/src/components/app-shell.tsx`（挂载抽屉）
- Test: `frontend/src/components/__tests__/page-image-locator.test.tsx`

**Interfaces:**
- Consumes: Task 4 的 `api.locateChunk` / `api.documentPageImage` / `toPercentBox`
- Produces:
  - `useHighlightDrawerStore`（`open(documentId, chunkId, title)` / `close()` / 状态）
  - `<HighlightDrawer />` 无 props，自取 store
  - `<PageImageLocator loc={PageImageLocator} documentId={string} />`

- [ ] **Step 1: 写失败的测试**

创建 `frontend/src/components/__tests__/page-image-locator.test.tsx`：

```tsx
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PageImageLocatorView } from "@/components/context/locators/page-image-locator";
import type { PageImageLocator } from "@/lib/locator";

vi.mock("@/lib/api", () => ({
  api: {
    documentPageImage: vi.fn(async () => new Blob(["x"], { type: "image/png" })),
  },
}));

// jsdom 不实现 createObjectURL
beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => "blob:fake");
  globalThis.URL.revokeObjectURL = vi.fn();
});

const loc: PageImageLocator = {
  kind: "page_image",
  page: 34,
  total_pages: 88,
  image_url: "/api/documents/d/pages/34/image",
  boxes: [
    [0.1, 0.2, 0.4, 0.25],
    [0.1, 0.26, 0.4, 0.31],
  ],
};

describe("PageImageLocatorView", () => {
  it("为每个框渲染一个高亮层，不合并", async () => {
    const { container } = render(
      <PageImageLocatorView loc={loc} documentId="d" />
    );
    await waitFor(() =>
      expect(container.querySelectorAll("[data-highlight-box]")).toHaveLength(2)
    );
  });

  it("显示页码与总页数", async () => {
    render(<PageImageLocatorView loc={loc} documentId="d" />);
    await waitFor(() => expect(screen.getByText(/第 34 页/)).toBeTruthy());
    expect(screen.getByText(/共 88 页/)).toBeTruthy();
  });

  it("卸载时释放 objectURL", async () => {
    const { unmount } = render(<PageImageLocatorView loc={loc} documentId="d" />);
    await waitFor(() => expect(globalThis.URL.createObjectURL).toHaveBeenCalled());
    unmount();
    expect(globalThis.URL.revokeObjectURL).toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/components/__tests__/page-image-locator.test.tsx`
Expected: FAIL — 模块不存在

- [ ] **Step 3: 写页内高亮渲染器**

创建 `frontend/src/components/context/locators/page-image-locator.tsx`：

```tsx
"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { toPercentBox, type PageImageLocator } from "@/lib/locator";

/**
 * 原页底图 + 归一化高亮层。
 *
 * 底图是干净的整页图（不带框），框是独立的一层 —— 这样同一页被多个引用命中时
 * 图只取一次，缩放时框自动跟随。
 */
export function PageImageLocatorView({
  loc,
  documentId,
}: {
  loc: PageImageLocator;
  documentId: string;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let revoked: string | null = null;
    let cancelled = false;
    api
      .documentPageImage(documentId, loc.page)
      .then((blob) => {
        if (cancelled) return;
        revoked = URL.createObjectURL(blob);
        setUrl(revoked);
      })
      .catch(() => {
        if (!cancelled) setError("原页渲染失败");
      });
    return () => {
      cancelled = true;
      if (revoked) URL.revokeObjectURL(revoked);
    };
  }, [documentId, loc.page]);

  if (error) {
    return <p className="p-4 text-sm text-muted-foreground">{error}</p>;
  }

  return (
    <div className="space-y-2">
      <p className="text-[11px] text-muted-foreground">
        第 {loc.page} 页 · 共 {loc.total_pages} 页
      </p>
      <div className="relative w-full overflow-hidden rounded-lg border border-border">
        {url ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={url} alt={`第 ${loc.page} 页`} className="block w-full" />
        ) : (
          <div className="aspect-[1/1.414] w-full animate-pulse bg-muted" />
        )}
        {url &&
          loc.boxes.map((box, i) => (
            <span
              key={i}
              data-highlight-box
              className="pointer-events-none absolute bg-amber-300/40 ring-1 ring-amber-500/60"
              style={toPercentBox(box)}
            />
          ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: 写 store 与抽屉壳**

创建 `frontend/src/stores/highlight-drawer-store.ts`：

```ts
import { create } from "zustand";

/**
 * 原文定位抽屉的开关状态。只存"看哪个 chunk"这三样，不存 locator 本身 ——
 * 抽屉每次打开都重新取，避免旧位置残留到下一次点击。
 */
interface HighlightDrawerState {
  open: boolean;
  documentId: string | null;
  chunkId: string | null;
  title: string;
  show: (documentId: string, chunkId: string, title: string) => void;
  close: () => void;
}

export const useHighlightDrawerStore = create<HighlightDrawerState>((set) => ({
  open: false,
  documentId: null,
  chunkId: null,
  title: "",
  show: (documentId, chunkId, title) =>
    set({ open: true, documentId, chunkId, title }),
  close: () => set({ open: false, documentId: null, chunkId: null, title: "" }),
}));
```

创建 `frontend/src/components/context/highlight-drawer.tsx`。**先读 `frontend/src/components/ui/sheet.tsx` 确认它导出的组件名与 props**（本项目基于 shadcn，通常是 `Sheet` / `SheetContent` / `SheetHeader` / `SheetTitle`），按它的实际 API 写：

```tsx
"use client";

import { useEffect, useState } from "react";
import { X } from "lucide-react";

import { api } from "@/lib/api";
import { ApiError } from "@/lib/api-client";
import { useHighlightDrawerStore } from "@/stores/highlight-drawer-store";
import type { Locator } from "@/lib/locator";
import { PageImageLocatorView } from "@/components/context/locators/page-image-locator";
import { SheetRangeLocatorView } from "@/components/context/locators/sheet-range-locator";
import { SlideLocatorView } from "@/components/context/locators/slide-locator";
import { TextRangeLocatorView } from "@/components/context/locators/text-range-locator";

function renderLocator(loc: Locator, documentId: string) {
  switch (loc.kind) {
    case "page_image":
      return <PageImageLocatorView loc={loc} documentId={documentId} />;
    case "sheet_range":
      return <SheetRangeLocatorView loc={loc} />;
    case "slide":
      return <SlideLocatorView loc={loc} />;
    case "text_range":
      return <TextRangeLocatorView loc={loc} />;
  }
}

export function HighlightDrawer() {
  const { open, documentId, chunkId, title, close } = useHighlightDrawerStore();
  const [loc, setLoc] = useState<Locator | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !documentId || !chunkId) return;
    let cancelled = false;
    setLoc(null);
    setMessage(null);
    api
      .locateChunk(documentId, chunkId)
      .then((next) => {
        if (!cancelled) setLoc(next);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setMessage(
          err instanceof ApiError && err.code === "no_locator"
            ? "该文档无法定位到原文位置（原件格式不含坐标，或这个片段入库时没有记录位置）"
            : "原文位置加载失败"
        );
      });
    return () => {
      cancelled = true;
    };
  }, [open, documentId, chunkId]);

  if (!open) return null;

  return (
    <aside className="fixed right-0 top-0 z-50 flex h-full w-[min(720px,60vw)] flex-col border-l border-border bg-background shadow-xl">
      <header className="flex items-center justify-between border-b border-border px-4 py-3">
        <span className="min-w-0 truncate text-sm font-medium">{title}</span>
        <button type="button" onClick={close} aria-label="关闭" className="text-muted-foreground hover:text-foreground">
          <X className="h-4 w-4" />
        </button>
      </header>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        {message && <p className="text-sm text-muted-foreground">{message}</p>}
        {!message && !loc && <p className="text-sm text-muted-foreground">正在定位原文…</p>}
        {loc && documentId && renderLocator(loc, documentId)}
      </div>
    </aside>
  );
}
```

> **注意**：本任务只建 `page-image-locator.tsx`。另外三个渲染器在 Task 6/7/8 里建；为了让抽屉能编译，**本任务先把它们建成返回"未知位置类型"的最小占位组件**，后续任务替换实现。

- [ ] **Step 5: Sources 卡片加入口**

修改 `frontend/src/components/context/sources-tab.tsx`：`SourceRow` 里，在行信息区之后加

```tsx
      {c.chunk_id && c.document_id && c.source_type !== "web" && (
        <button
          type="button"
          className="mt-1.5 text-[11px] font-medium text-indigo-600 hover:underline dark:text-indigo-400"
          onClick={() =>
            useHighlightDrawerStore
              .getState()
              .show(c.document_id as string, c.chunk_id as string, c.document_name || "原文位置")
          }
        >
          查看原文位置
        </button>
      )}
```

并加 import `import { useHighlightDrawerStore } from "@/stores/highlight-drawer-store";`

按钮**只在有 chunk_id 时出现**：没有 chunk 锚点就没有可定位的对象。**不预判能力**——能不能定位由点击后的响应决定（不支持时给明确文案，而不是把按钮藏起来让人以为没做）。

- [ ] **Step 6: 挂载抽屉**

在 `frontend/src/components/app-shell.tsx` 里渲染 `<HighlightDrawer />`（与既有顶层面板同级），加 import。

- [ ] **Step 7: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/components/__tests__/page-image-locator.test.tsx`
Expected: PASS（3 项）

- [ ] **Step 8: typecheck + lint + 提交**

```bash
cd frontend && npm run typecheck && npm run lint
git add src/lib/locator.ts src/lib/api.ts src/stores/highlight-drawer-store.ts src/components/context/ src/components/app-shell.tsx src/components/__tests__/
git commit -m "feat(web): 原文定位抽屉 + 页内高亮渲染 + Sources 入口"
```

---

### Task 6: 文本片段渲染器（覆盖 DOCX / RTF / TXT / MD / HTML / EPUB）

**Files:**
- Modify: `frontend/src/components/context/locators/text-range-locator.tsx`（替换占位）
- Test: `frontend/src/components/__tests__/text-range-locator.test.tsx`

**Interfaces:**
- Consumes: Task 4 的 `splitTextRange`、`TextRangeLocator`
- Produces: `<TextRangeLocatorView loc={TextRangeLocator} />`

- [ ] **Step 1: 写失败的测试**

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TextRangeLocatorView } from "@/components/context/locators/text-range-locator";

describe("TextRangeLocatorView", () => {
  it("高亮片段落在 <mark> 里，且原文一字不差", () => {
    const loc = {
      kind: "text_range" as const,
      text: "前文命中后文",
      start: 2,
      end: 4,
    };
    const { container } = render(<TextRangeLocatorView loc={loc} />);

    const mark = container.querySelector("mark");
    expect(mark?.textContent).toBe("命中");
    expect(container.textContent).toBe("前文命中后文");
  });

  it("偏移越界时不丢字符、不抛错", () => {
    const loc = { kind: "text_range" as const, text: "短", start: 99, end: 120 };
    const { container } = render(<TextRangeLocatorView loc={loc} />);
    expect(container.textContent).toBe("短");
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/components/__tests__/text-range-locator.test.tsx`
Expected: FAIL

- [ ] **Step 3: 实现**

```tsx
"use client";

import { splitTextRange, type TextRangeLocator } from "@/lib/locator";

/**
 * 兜底层：把原文窗口里的命中片段标出来。
 *
 * 这一层对任何有文本的格式都成立 —— Word、RTF、纯文本、HTML、EPUB 全部走它。
 * 它不是"降级凑数"：对没有分页概念的格式，"原文的哪一段"就是唯一诚实的答案。
 */
export function TextRangeLocatorView({ loc }: { loc: TextRangeLocator }) {
  const [before, hit, after] = splitTextRange(loc);
  return (
    <div className="space-y-2">
      <p className="text-[11px] text-muted-foreground">原文片段（该格式不含版面坐标）</p>
      <pre className="whitespace-pre-wrap break-words rounded-lg border border-border bg-muted/30 p-3 text-xs leading-relaxed">
        {before}
        <mark className="bg-amber-300/60 px-0.5">{hit}</mark>
        {after}
      </pre>
    </div>
  );
}
```

- [ ] **Step 4: 跑测试确认通过并提交**

```bash
cd frontend && npx vitest run src/components/__tests__/text-range-locator.test.tsx && npm run typecheck
git add src/components/context/locators/text-range-locator.tsx src/components/__tests__/text-range-locator.test.tsx
git commit -m "feat(web): 原文文本片段高亮渲染器（覆盖全部无坐标格式）"
```

---

### Task 7: 表格行布局与高亮（XLSX / XLS / CSV）

**Files:**
- Modify: `backend/app/rag/parsers.py`（`_parse_table` 产出 sheets 布局）
- Test: `backend/tests/test_layout_sheets.py`
- Modify: `frontend/src/components/context/locators/sheet-range-locator.tsx`（替换占位）
- Test: `frontend/src/components/__tests__/sheet-range-locator.test.tsx`

**Interfaces:**
- Consumes: Task 1 的 `SheetLayout` / `SheetRow` / `page_bases`；Task 2 的 `_locate_sheet`
- Produces: `ParsedDocument.layout.sheets`；`<SheetRangeLocatorView loc={SheetRangeLocator} />`

- [ ] **Step 1: 写后端失败的测试**

```python
"""表格的逐行字符区间：让 chunk 能落到具体行上。"""
from __future__ import annotations

from app.rag.layout import page_bases
from app.rag.parsers import default_parser


def _make_xlsx(path) -> str:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["序号", "条款", "要求"])
    for i in range(5):
        ws.append([str(i + 1), f"8.5.1-{i}", f"要求内容 {i}"])
    wb.save(str(path))
    return str(path)


def test_xlsx_layout_rows_cover_every_rendered_line(tmp_path):
    parsed = default_parser.parse(_make_xlsx(tmp_path / "a.xlsx"), ".xlsx")

    assert parsed.layout is not None
    assert len(parsed.layout.sheets) == 1
    sheet = parsed.layout.sheets[0]
    assert sheet.name == "Sheet1"
    # 表头 + 5 行数据
    assert len(sheet.rows) == 6
    # 每一行的区间必须指向 text 里那一行
    lines = parsed.text.splitlines()
    for row in sheet.rows:
        assert parsed.text[row.char_start : row.char_end] == lines[row.row]


def test_xlsx_layout_text_is_unchanged(tmp_path):
    """加布局不得改变 text。"""
    src = _make_xlsx(tmp_path / "b.xlsx")
    parsed = default_parser.parse(src, ".xlsx")
    assert "条款" in parsed.text and "要求内容 4" in parsed.text


def test_page_bases_skips_empty_pages():
    bases = page_bases(["aa", "", "bbb"])
    assert bases == {1: 0, 3: 4}   # "aa" + "\n\n" + "bbb"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_layout_sheets.py -q --tb=short`
Expected: FAIL — `parsed.layout` 为 `None`

- [ ] **Step 3: `_parse_table` 产出布局**

`_parse_table` 里，渲染每个 sheet 的文本时同步记行偏移。`_df_to_text` 目前返回整块字符串，改为在其上包一层按行计偏移：

```python
def _df_to_text_with_rows(df: Any, sheet: str | None = None) -> tuple[str, list[tuple[int, int]]]:
    """``_df_to_text`` 的带偏移版本：返回 (整块文本, 每行的 (start, end))。

    行偏移相对**整块**文本（含 sheet 头），调用方再叠加各块在全文里的基准。
    """
    text = _df_to_text(df, sheet=sheet)
    spans: list[tuple[int, int]] = []
    cursor = 0
    for line in text.splitlines():
        spans.append((cursor, cursor + len(line)))
        cursor += len(line) + 1  # "\n"
    return text, spans
```

`_parse_table` 的 workbook 分支改为：

```python
        sheets = xl.sheet_names
        frames: list[str] = []
        sheet_layouts: list[SheetLayout] = []
        for s in sheets:
            block, spans = _df_to_text_with_rows(pd.read_excel(xl, sheet_name=s), sheet=s)
            frames.append(block)
            sheet_layouts.append((s, spans))
    # ...（关闭 xl）
    text = "\n\n".join(frames)
    bases: list[int] = []
    cursor = 0
    for i, block in enumerate(frames):
        if i:
            cursor += 2
        bases.append(cursor)
        cursor += len(block)
```

然后：

```python
    layout = DocumentLayout(
        sheets=tuple(
            SheetLayout(
                name=name,
                rows=tuple(
                    SheetRow(row=i, char_start=bases[bi] + s, char_end=bases[bi] + e)
                    for i, (s, e) in enumerate(spans)
                ),
            )
            for bi, (name, spans) in enumerate(sheet_layouts)
        )
    )
```

并 `layouts=...` 传进 `ParsedDocument`。**csv 分支也要同样处理**（它是 `frames` 的单块版本），否则 CSV 落到 `text_range`——那不算错，但既然 Workbook 分支做了，CSV 不做会显得是漏的。csv 分支：

```python
        block, spans = _df_to_text_with_rows(df)
        layout = DocumentLayout(
            sheets=(
                SheetLayout(
                    name="csv",
                    rows=tuple(
                        SheetRow(row=i, char_start=s, char_end=e)
                        for i, (s, e) in enumerate(spans)
                    ),
                ),
            )
        )
```

- [ ] **Step 4: 跑后端测试确认通过**

Run: `cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_layout_sheets.py tests/test_locator.py -q --tb=short`
Expected: PASS

- [ ] **Step 5: 跑既有表格解析回归**

Run: `cd backend && ./.venv/Scripts/python.exe -m pytest tests -q -k "table or csv or xlsx or preview" --tb=short`
Expected: PASS

- [ ] **Step 6: 写前端测试并实现**

```tsx
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SheetRangeLocatorView } from "@/components/context/locators/sheet-range-locator";

const loc = {
  kind: "sheet_range" as const,
  sheet: "Sheet1",
  header: ["序号", "条款"],
  rows: [
    ["1", "8.5.1"],
    ["2", "8.5.2"],
    ["3", "8.5.3"],
  ],
  highlight: [1, 3] as [number, number],
};

describe("SheetRangeLocatorView", () => {
  it("只高亮命中行，其他行不高亮", () => {
    const { container } = render(<SheetRangeLocatorView loc={loc} />);
    const marked = container.querySelectorAll("[data-row-hit]");
    expect(marked).toHaveLength(2);
    expect(marked[0].textContent).toContain("8.5.2");
  });

  it("渲染全部行与表头", () => {
    const { container } = render(<SheetRangeLocatorView loc={loc} />);
    expect(container.querySelectorAll("tbody tr")).toHaveLength(3);
    expect(container.querySelectorAll("thead th")).toHaveLength(2);
  });
});
```

实现：

```tsx
"use client";

import { highlightedRows, type SheetRangeLocator } from "@/lib/locator";

/** 表格的命中行高亮。行号来自后端按 chunk 字符区间算出的相交行。 */
export function SheetRangeLocatorView({ loc }: { loc: SheetRangeLocator }) {
  const hits = new Set(highlightedRows(loc));
  return (
    <div className="space-y-2">
      <p className="text-[11px] text-muted-foreground">工作表：{loc.sheet}</p>
      <div className="overflow-auto rounded-lg border border-border">
        <table className="w-full border-collapse text-xs">
          <thead>
            <tr className="bg-muted/50">
              {loc.header.map((cell, i) => (
                <th key={i} className="border-b border-border px-2 py-1.5 text-left font-medium">
                  {cell}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loc.rows.map((row, i) => (
              <tr
                key={i}
                {...(hits.has(i) ? { "data-row-hit": "" } : {})}
                className={hits.has(i) ? "bg-amber-300/40" : undefined}
              >
                {row.map((cell, j) => (
                  <td key={j} className="border-b border-border/50 px-2 py-1">
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
```

- [ ] **Step 7: 跑测试并提交**

```bash
cd frontend && npx vitest run src/components/__tests__/sheet-range-locator.test.tsx && npm run typecheck
cd ../backend && ./.venv/Scripts/python.exe -m ruff check app tests
git add backend/app/rag/parsers.py backend/tests/test_layout_sheets.py frontend/src/components/context/locators/sheet-range-locator.tsx frontend/src/components/__tests__/sheet-range-locator.test.tsx
git commit -m "feat: 表格逐行布局 + 命中行高亮（xlsx/xls/csv）"
```

---

### Task 8: 幻灯片形状布局与高亮（PPTX）

**Files:**
- Modify: `backend/app/rag/parsers.py`（`_parse_pptx` 产出 slides 布局）
- Test: `backend/tests/test_layout_slides.py`
- Modify: `frontend/src/components/context/locators/slide-locator.tsx`（替换占位）
- Test: `frontend/src/components/__tests__/slide-locator.test.tsx`

**Interfaces:**
- Consumes: Task 1 的 `SlideLayout` / `ShapeBox`；Task 2 的 `_locate_slide`
- Produces: `ParsedDocument.layout.slides`；`<SlideLocatorView loc={SlideLocator} />`

- [ ] **Step 1: 写后端失败的测试**

```python
"""幻灯片的形状坐标：让 chunk 能落到具体形状上。"""
from __future__ import annotations

from app.rag.parsers import default_parser


def _make_pptx(path) -> str:
    from pptx import Presentation
    from pptx.util import Emu, Inches

    prs = Presentation()
    blank = prs.slide_layouts[6]
    for i in range(2):
        slide = prs.slides.add_slide(blank)
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
        box.text_frame.text = f"第{i}张幻灯片的标题内容"
        box2 = slide.shapes.add_textbox(Inches(1), Inches(3), Inches(4), Inches(1))
        box2.text_frame.text = f"第{i}张幻灯片的正文内容"
    prs.save(str(path))
    return str(path)


def test_pptx_layout_shapes_are_in_slide_bounds_and_ordered(tmp_path):
    parsed = default_parser.parse(_make_pptx(tmp_path / "a.pptx"), ".pptx")

    assert parsed.layout is not None
    assert len(parsed.layout.slides) == 2
    slide = parsed.layout.slides[0]
    assert slide.slide_no == 1
    assert slide.shapes, "应当提取到形状"
    for shape in slide.shapes:
        assert 0 <= shape.x < shape.x + shape.w <= slide.width
        assert 0 <= shape.y < shape.y + shape.h <= slide.height
        assert parsed.text[shape.char_start : shape.char_end] == shape.text


def test_pptx_layout_text_is_unchanged(tmp_path):
    parsed = default_parser.parse(_make_pptx(tmp_path / "b.pptx"), ".pptx")
    assert "第0张幻灯片的标题内容" in parsed.text
    assert parsed.text.startswith("# 幻灯片 1")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_layout_slides.py -q --tb=short`
Expected: FAIL — `parsed.layout` 为 `None`

- [ ] **Step 3: `_parse_pptx` 产出布局**

`_parse_pptx` 目前逐 slide 拼 `blocks` 列表，然后 `"\n".join(blocks)`。要记每个形状文本在**全文**里的偏移，就得在最后 join 完成后回填——因为在 join 之前不知道基准。

做法：拼 block 的同时记下"这个形状的文本是哪个 block"，join 之后再算偏移：

```python
    EMU_PER_PT = 12700  # 1pt = 12700 EMU
    slides: list[str] = []
    tables: list[str] = []
    # (slide_no, block_index, x, y, w, h) —— 形状在 blocks 里的下标，join 之后再定偏移
    pending_shapes: list[tuple[int, int, float, float, float, float]] = []
    slide_sizes: list[tuple[float, float]] = []
    for idx, slide in enumerate(prs.slides, 1):
        blocks: list[str] = [f"# 幻灯片 {idx}"]
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame:
                for para in shape.text_frame.paragraphs:
                    txt = "".join(run.text for run in para.runs).strip()
                    if txt:
                        blocks.append(txt)
                        # 形状框：EMU → pt
                        pending_shapes.append(
                            (
                                idx,
                                len(blocks) - 1,
                                float(shape.left) / EMU_PER_PT,
                                float(shape.top) / EMU_PER_PT,
                                float(shape.width) / EMU_PER_PT,
                                float(shape.height) / EMU_PER_PT,
                            )
                        )
            if getattr(shape, "has_table", False) and shape.has_table:
                tbl = shape.table
                rows = [[cell.text for cell in row.cells] for row in tbl.rows]
                rendered = _table_to_text(rows)
                if rendered.strip():
                    tables.append(rendered)
                    blocks.append(rendered)
        if getattr(slide, "has_notes_slide", False) and slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                blocks.append(f"[备注] {notes}")
        slides.append("\n".join(blocks))
        slide_sizes.append(
            (float(prs.slide_width) / EMU_PER_PT, float(prs.slide_height) / EMU_PER_PT)
        )
    text = "\n\n".join(slides)
```

然后 join 完成后回填偏移：

```python
    # slide 文本以 "\n\n" 相连，块内以 "\n" 相连 —— 走一遍 join 记账，偏移是算出来的
    slide_bases: list[int] = []
    cursor = 0
    for i, block in enumerate(slides):
        if i:
            cursor += 2
        slide_bases.append(cursor)
        cursor += len(block)
    block_offsets: list[list[int]] = []
    for block, base in zip(slides, slide_bases):
        offsets: list[int] = []
        local = 0
        for line in block.split("\n"):
            offsets.append(base + local)
            local += len(line) + 1
        block_offsets.append(offsets)

    by_slide: dict[int, list[ShapeBox]] = {}
    for slide_no, block_index, x, y, w, h in pending_shapes:
        at = block_offsets[slide_no - 1][block_index]
        line = slides[slide_no - 1].split("\n")[block_index]
        by_slide.setdefault(slide_no, []).append(
            ShapeBox(
                x=x, y=y, w=w, h=h, text=line,
                char_start=at, char_end=at + len(line),
            )
        )
    rendered_slides = [
        SlideLayout(
            slide_no=no,
            width=slide_sizes[no - 1][0],
            height=slide_sizes[no - 1][1],
            shapes=tuple(by_slide.get(no, [])),
        )
        for no in range(1, len(slides) + 1)
        if by_slide.get(no)
    ]
    layout = DocumentLayout(slides=tuple(rendered_slides)) if rendered_slides else None
```

并 `layout=layout` 传进 `ParsedDocument`。

- [ ] **Step 4: 跑后端测试确认通过**

Run: `cd backend && ./.venv/Scripts/python.exe -m pytest tests/test_layout_slides.py tests/test_locator.py -q --tb=short`
Expected: PASS

- [ ] **Step 5: 写前端测试并实现**

```tsx
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SlideLocatorView } from "@/components/context/locators/slide-locator";

const loc = {
  kind: "slide" as const,
  slide_no: 3,
  width: 960,
  height: 540,
  shapes: [
    { rect: [10, 10, 400, 60] as [number, number, number, number], text: "标题", hit: false },
    { rect: [10, 100, 400, 60] as [number, number, number, number], text: "命中正文", hit: true },
  ],
};

describe("SlideLocatorView", () => {
  it("只有命中的形状带 data-shape-hit", () => {
    const { container } = render(<SlideLocatorView loc={loc} />);
    const hits = container.querySelectorAll("[data-shape-hit]");
    expect(hits).toHaveLength(1);
    expect(hits[0].textContent).toContain("命中正文");
  });

  it("用 viewBox 表达幻灯片尺寸", () => {
    const { container } = render(<SlideLocatorView loc={loc} />);
    expect(container.querySelector("svg")?.getAttribute("viewBox")).toBe("0 0 960 540");
  });
});
```

实现（SVG 缩放由 `viewBox` 负责，形状坐标直接用 pt，不需要归一化）：

```tsx
"use client";

import type { SlideLocator } from "@/lib/locator";

/** 幻灯片形状高亮。坐标是 pt，由 viewBox 负责等比缩放到容器宽度。 */
export function SlideLocatorView({ loc }: { loc: SlideLocator }) {
  return (
    <div className="space-y-2">
      <p className="text-[11px] text-muted-foreground">第 {loc.slide_no} 张幻灯片</p>
      <svg
        viewBox={`0 0 ${loc.width} ${loc.height}`}
        className="w-full rounded-lg border border-border bg-white"
        role="img"
        aria-label={`第 ${loc.slide_no} 张幻灯片`}
      >
        {loc.shapes.map((shape, i) => {
          const [x, y, w, h] = shape.rect;
          return (
            <g key={i} {...(shape.hit ? { "data-shape-hit": "" } : {})}>
              <rect
                x={x}
                y={y}
                width={w}
                height={h}
                rx={2}
                fill={shape.hit ? "rgba(252, 211, 77, 0.55)" : "rgba(0,0,0,0.04)"}
                stroke={shape.hit ? "rgba(217, 119, 6, 0.9)" : "rgba(0,0,0,0.12)"}
              />
              <foreignObject x={x} y={y} width={w} height={h}>
                <div className="h-full w-full overflow-hidden p-1 text-[10px] leading-tight">
                  {shape.text}
                </div>
              </foreignObject>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
```

- [ ] **Step 6: 跑测试并提交**

```bash
cd frontend && npx vitest run src/components/__tests__/slide-locator.test.tsx && npm run typecheck
cd ../backend && ./.venv/Scripts/python.exe -m ruff check app tests
git add backend/app/rag/parsers.py backend/tests/test_layout_slides.py frontend/src/components/context/locators/slide-locator.tsx frontend/src/components/__tests__/slide-locator.test.tsx
git commit -m "feat: 幻灯片形状布局 + 命中形状高亮（pptx）"
```

---

### Task 9: 全量验收

**Files:** 无新增

- [ ] **Step 1: 后端全量**

Run:
```bash
cd backend && ./.venv/Scripts/python.exe -m ruff check app tests && ./.venv/Scripts/python.exe -m pytest tests -q --tb=short \
  --deselect tests/test_agent_phase2.py::test_agent_mode_emits_plan_created \
  --deselect tests/test_agent_phase5.py::test_full_native_agent_path \
  --deselect tests/test_agent_graph_lifecycle.py::test_multi_agent_approval_pauses_then_resumes
```
Expected: 全绿。若出现 `OperationalError` 批量失败，先确认死锁用例是否被 deselect 掉。

- [ ] **Step 2: 前端全量**

Run: `cd frontend && npm run typecheck && npm run lint && npm run test`
Expected: 全绿

- [ ] **Step 3: 真机验收**

起本地前后端，上传一份**真 PDF**、一份 xlsx、一份 pptx、一份 docx，各提问一句触发引用，点「查看原文位置」：

- PDF → 抽屉出原页，黄框**精确覆盖该 chunk 的行**，且不覆盖相邻 chunk 的行
- xlsx → 命中行高亮
- pptx → 命中形状高亮
- docx → 原文片段 `<mark>`
- 找一个 2026-09-19 之后入库的**老** PDF 引用 → 同样能高亮，**没有做任何回填**

- [ ] **Step 4: 提计划外的修正**

若真机验收发现定位偏移，**先写一个能复现它的后端测试**再改，不要在没断言的情况下调坐标。

---

## 自查记录

**Spec 覆盖**：§4.1 数据流 → Task 1/3；§4.3 模块边界 → Task 1/2/3/4；§5.1 解析层 → Task 1/7/8；§5.2 定位层 → Task 2；§5.3 端点层 → Task 3；§5.4 前端 → Task 4/5/6/7/8；§6 测试策略 → 各任务内；§7.1 降级 → Task 2 的 `None` 分支 + Task 5 的文案；§8 验收 → Task 9。

**已知缺口（有意为之）**：§7.3 PyMuPDF 内存上限只做了页面积与 dpi 限幅，没做进程级隔离——PyMuPDF 崩溃会带走 worker 进程。这是既有风险（`/preview` 今天就在重解析不可信 PDF），本计划不扩大它，也不在本轮修。

**类型一致性**：`locate()` 的关键字参数在 Task 2 定义为 `(parsed, *, page, char_start, char_end, text_context=)`，Task 3 的调用点一致；`SheetRangeLocator.highlight` 是 `tuple[int,int]`，前端类型是 `[number, number] | null`（JSON 把 tuple 序列化成数组，null 覆盖字段缺失）；`ShapeBox`（后端 layout）与 `SlideShape`（后端 locator）是两个类型，前端只见到后者序列化后的 `{rect, text, hit}`。

---

## 执行偏差（实施后回填，供后续照此执行的人参考）

计划里以下五处在实施中被证明是错的或有缺的，代码按修正后的做法落地：

1. **前端测试栈**：计划写的是 `@testing-library/react` + `render()`/`screen`/`container.querySelectorAll`。
   仓库**没有装 testing-library，也没有 jsdom**（`vitest.config.ts` 是 `environment: "node"`，
   既有组件测试一律 `renderToString`）。四个渲染器因此都做成**纯 props 进、纯 DOM 出**的组件，
   取数逻辑单独留在 `PageImageLocatorView` 里；测试改为 `renderToString` + 字符串断言，
   落在 `src/components/__tests__/locator-views.test.tsx`（一个文件覆盖四个渲染器）。

2. **表格的表头不在第 0 行**：`_df_to_text(df, sheet=…)` 会在块首插一行 `## Sheet: 名称`，
   所以 xlsx 的表头在第 1 行、csv 在第 0 行。计划里 `_locate_sheet` 写死 `lines[0]` 当表头，
   在 xlsx 上会让整张表列错位。修正：`SheetLayout` 增加 `header_line` 字段，由
   `_df_to_text_with_rows` 算出交给下游；`SheetRow.row` 改为**数据行**下标（表头不算行），
   `highlight` 因此不需要再减表头。

3. **归一化换算会产生浮点尾巴**：`0.6 - 0.5` 是 `0.09999999999999998`，直接乘 100 会写出
   `9.999999999999998%`。`percent()` 里取到小数后 4 位。不这么做的话每个断言和快照都会被
   浮点噪声弄脏。

4. **ETag 不能用 `doc.content_sha256`**（计划里写的）：`Document` 模型**没有这一列**。
   改用文件本身的 `(路径, 大小, mtime_ns)` 做指纹——`doc.id` 不够：重索引/存储迁移会覆盖
   同一个 `file_path` 而文档行不变，那时 id 不变但图已经变了。

5. **`test_locator.py` 里那条 sheet 断言的期望值本身算错了**（计划写 `(1, 3)`，实际语义是
   `(0, 2)`）。修正 2 之后 `SheetRow.row` 的语义变了，测试夹具也一并改成"跳过表头、数据行从 0 编号"。

另有一处**未按计划做**：Task 9 Step 3 的"真机验收"（起前后端、上传真 PDF/xlsx/pptx/docx 各点一遍）
在本轮**没有执行**——见最终报告。它被 `test_document_locator_api.py` 里那条
"真解析 → 定位 → HTTP 返回归一化框"的端到端用例部分覆盖，但浏览器里的实际观感（框是否精确盖住
正文、抽屉宽度是否够读）仍需人工确认。

6. **计划漏了一个前置缺陷：`chunk_meta._offsets` 在切块接缝上大面积丢锚点。**
   计划的全局约束写着"`char_start/char_end` 已在库里、不需要回填"，但没验证这句话在
   **真切块器**下是否成立。实测（3 页 PDF、默认 500/80 切分）：3 个 chunk 只有 1 个
   拿到锚点。原因有二：
   - 重叠窗口跨过分页接缝时，原文的 `"\n\n"` 在 chunk 里被归一成 `"\n"`；
   - 更狠的一种：两页的行被**直接粘成一个 token**（分隔符整个消失），`A-0-29P1`。

   于是 `text.find(chunk)` 失败 → `char_start=None` → 同时丢掉页号、heading 与原文
   定位。既有测试 `test_annotate_recovers_page_numbers` 喂的是**精确切片**（`[page1, page2]`），
   所以从未覆盖真切块器的输出 —— 这是缺陷长期没被发现的原因。

   修法：`_offsets` 保留精确 `find` 快路径（已能定位的 chunk 语义一字不变），
   新增一条**忽略空白**的慢路径 —— 把原文与 chunk 都去掉空白后按字符匹配，再用
   位置映射回原文坐标。选"非空白字符"而不是"token"，正是因为 token 方案对
   "两个 token 粘成一个"无能为力（实施中先写了 token 版，实测仍然全灭，才换掉）。
   两级都失败仍然是 `None`，不猜。新增 `tests/test_chunk_meta_seams.py` 盯住它。

   **注意**：这个修复只对**新入库**的文档生效。已入库的老文档当初存下的就是
   `None`，本轮不做回填（与"老文档自动可用"的结论叠加后，老文档的可用率取决于
   它入库时有没有踩到接缝）。
