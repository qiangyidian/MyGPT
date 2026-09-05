"""Structured preview rendering.

The ingestion parsers already produce structured results (``pages`` per
PDF-page/sheet/slide, ``tables`` as ``col | col`` row text) — but the flat
``text`` they fold everything into is what preview used to show, which reads
badly for tables (pipe rows with no visual structure) and gives no page
boundaries. This module re-renders the SAME parsed data as GitHub-Flavored
Markdown so the existing chat Markdown renderer gives users a real preview:
paginated PDFs, true tables for csv/xlsx/docx-tables, per-slide sections for
pptx.

The rendered preview is a VIEW over parsed.text (same parser, same content) —
chunking/embedding are untouched.
"""
from __future__ import annotations

import re

from app.rag.base import ParsedDocument

# A table row from _table_to_text/_df_to_text: "a | b | c". Pipes inside
# cells were already replaced with "/" by the parsers, so a split on " | "
# is safe.
_ROW_RE = re.compile(r"\s\|\s")

# pandas fabricates these headers when a sheet has no header row.
_UNNAMED_COL = re.compile(r"^Unnamed:\s*\d+$")


def _rows_to_gfm(rows: list[list[str]], max_rows: int = 200) -> str:
    """Render a list of rows as a GFM table (first row = header)."""
    if not rows:
        return ""
    rows = rows[: max_rows + 1]  # header + max_rows body rows
    header, *body = rows
    width = len(header)
    def norm(r: list[str]) -> list[str]:
        return (r + [""] * width)[:width]

    def cells(r: list[str]) -> list[str]:
        return [c.replace("|", "/").replace("\n", " ").strip() for c in r]
    out = ["| " + " | ".join(cells(header)) + " |",
           "|" + "---|" * width]
    for r in body:
        out.append("| " + " | ".join(cells(norm(r))) + " |")
    if len(rows) > max_rows + 1:
        out.append(f"\n*（表格过长，仅显示前 {max_rows} 行）*")
    return "\n".join(out)


def _pipe_block_to_gfm(block: str, max_rows: int = 200) -> str:
    """Convert a ``col | col`` text block (parser table output) to a GFM table."""
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    rows = [_ROW_RE.split(ln) for ln in lines]
    if not rows:
        return ""
    # A header with no body rows (PyMuPDF sometimes only catches the header
    # of a web-rendered table; the data rows stay in the page text) renders
    # as an empty frame — skip it.
    if len(rows) < 2:
        return ""
    # Clean pandas "Unnamed: N" headers into something readable.
    header = rows[0]
    if any(_UNNAMED_COL.match(h.strip()) for h in header):
        rows = [["列" + str(i + 1) for i in range(len(header))], *rows[1:]]
    return _rows_to_gfm(rows, max_rows)


def _has_tables(parsed: ParsedDocument) -> bool:
    return bool(parsed.tables and any(t.strip() for t in parsed.tables))


def _render_pdf(parsed: ParsedDocument) -> str:
    sections: list[str] = []
    pages = parsed.pages or []
    if pages:
        for i, page in enumerate(pages, 1):
            body = page.strip()
            if not body:
                sections.append(f"## 第 {i} 页\n\n*（本页无可提取文本，可能为图片/扫描页）*")
            else:
                sections.append(f"## 第 {i} 页\n\n{body}")
    else:
        sections.append(parsed.text or "")
    if _has_tables(parsed):
        rendered = "\n\n".join(
            gfm for gfm in (_pipe_block_to_gfm(t) for t in parsed.tables if t.strip()) if gfm
        )
        if rendered:
            sections.append(f"## 提取的表格\n\n{rendered}")
    return "\n\n---\n\n".join(s for s in sections if s.strip())


_SHEET_RE = re.compile(r"^##\s+Sheet:\s*(.+)$", re.MULTILINE)


def _render_table(parsed: ParsedDocument) -> str:
    """csv/xlsx/xls: one GFM table per sheet (blocks already carry sheet headers)."""
    blocks = [b for b in (parsed.pages or parsed.tables or []) if b and b.strip()]
    sheets = (parsed.metadata or {}).get("sheets") or []
    sections: list[str] = []
    # Each block starts with "## Sheet: name" (workbooks) or is the whole csv.
    cursor = 0
    for block in blocks:
        m = _SHEET_RE.search(block)
        name = m.group(1).strip() if m else (sheets[cursor] if cursor < len(sheets) else None)
        cursor += 1
        body = _SHEET_RE.sub("", block, count=1).strip()
        lines = [ln for ln in body.splitlines() if ln.strip()]
        if not lines:
            continue
        rows = [_ROW_RE.split(ln.strip()) for ln in lines]
        if len(rows) < 2:  # header-only block — nothing to tabulate
            continue
        header = rows[0]
        if any(_UNNAMED_COL.match(h.strip()) for h in header):
            rows = [["列" + str(i + 1) for i in range(len(header))], *rows[1:]]
        title = f"# {name}" if name else ""
        gfm = _rows_to_gfm(rows)
        sections.append((title + "\n\n" if title else "") + gfm)
    return "\n\n---\n\n".join(sections) if sections else (parsed.text or "")


def _render_docx(parsed: ParsedDocument) -> str:
    """Word: paragraphs as-is, real tables as GFM tables.

    Word authors often use 1×1 tables as styled text boxes (tips, code
    blocks); those parse as single-row "tables". A single-row block is not a
    table — keep it as body text. Multi-row blocks render as GFM tables.

    Ordering: the parser folds ALL tables after the paragraphs in ``text``
    (document order between paragraphs and tables is not preserved by
    python-docx's split iteration), so we rebuild: paragraphs, then 1×1
    text-box content in order, then real tables.
    """
    tables = [t for t in (parsed.tables or []) if t.strip()]
    body = parsed.text or ""
    if tables:
        # Strip the folded pipe-blocks off the tail of text.
        tail = ("\n\n" + "\n\n".join(tables)).strip()
        if tail and body.endswith(tail):
            body = body[: -len(tail)].rstrip()

    sections: list[str] = []
    if body.strip():
        sections.append(body)
    for t in tables:
        gfm = _pipe_block_to_gfm(t)
        if gfm:
            sections.append(gfm)
        else:
            # 1×1 styled text box (or header-only fragment): keep the text.
            txt = "\n".join(ln for ln in t.splitlines() if ln.strip())
            if txt:
                sections.append(txt)
    return "\n\n".join(sections)


def _render_pptx(parsed: ParsedDocument) -> str:
    # pages already carry "# 幻灯片 N" headers + [备注]; render as-is (it IS md).
    return parsed.text or ""


def render_preview_markdown(parsed: ParsedDocument, file_type: str) -> str:
    """Format a parsed document as preview-friendly GFM Markdown."""
    ft = (file_type or "").lower()
    if ft == ".pdf":
        return _render_pdf(parsed)
    if ft in (".csv", ".xlsx", ".xls", ".ods"):
        return _render_table(parsed)
    if ft in (".docx", ".doc", ".odt"):
        return _render_docx(parsed)
    if ft in (".pptx", ".ppt", ".odp"):
        return _render_pptx(parsed)
    return parsed.text or ""
