"""Document parsers.

One dispatcher (``parse_file``) selects a parser by file extension. Each parser
returns plain extracted text. Heavy/optional libs are imported lazily so the app
still imports cleanly on a minimal install; a missing parser surfaces a clear error.
"""
from __future__ import annotations

import csv
import io
from typing import Callable

from app.rag.base import DocumentParser


def _parse_txt(path: str, _ext: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        return fh.read()


def _parse_markdown(path: str, _ext: str) -> str:
    """Render Markdown to plain text (strip HTML tags from the rendered HTML)."""
    try:
        import markdown as md  # type: ignore
        from bs4 import BeautifulSoup  # type: ignore
    except Exception:
        # Without the libs, fall back to raw text — still usable for chunking.
        return _parse_txt(path, _ext)
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        html = md.markdown(fh.read())
    return BeautifulSoup(html, "html.parser").get_text(separator="\n")


def _parse_pdf(path: str, _ext: str) -> str:
    try:
        import pdfplumber  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dep
        raise ValueError(f"pdfplumber 未安装，无法解析 PDF: {exc}") from exc
    pages: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            if text:
                pages.append(text)
    return "\n\n".join(pages)


def _parse_docx(path: str, _ext: str) -> str:
    try:
        import docx  # type: ignore  # python-docx
    except Exception as exc:  # pragma: no cover - optional dep
        raise ValueError(f"python-docx 未安装，无法解析 Word: {exc}") from exc
    doc = docx.Document(path)
    return "\n".join(p.text for p in doc.paragraphs if p.text and p.text.strip())


def _parse_table(path: str, ext: str) -> str:
    """Render CSV/XLSX/XLS into a flattened text blob (headers + rows)."""
    try:
        import pandas as pd  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dep
        raise ValueError(f"pandas 未安装，无法解析表格: {exc}") from exc
    if ext == ".csv":
        df = pd.read_csv(path)
    else:
        df = pd.read_excel(path)
    # Serialize each row as "col: value; ..." so embeddings capture content.
    lines: list[str] = []
    cols = list(df.columns)
    lines.append(" | ".join(str(c) for c in cols))
    for _, row in df.iterrows():
        lines.append(" | ".join("" if pd.isna(v) else str(v) for v in row.tolist()))
    return "\n".join(lines)


_EXT_TO_PARSER: dict[str, Callable[[str, str], str]] = {
    ".txt": _parse_txt,
    ".md": _parse_markdown,
    ".markdown": _parse_markdown,
    ".pdf": _parse_pdf,
    ".docx": _parse_docx,
    ".doc": _parse_docx,
    ".csv": _parse_table,
    ".xlsx": _parse_table,
    ".xls": _parse_table,
}


class DefaultDocumentParser(DocumentParser):
    """Dispatch parser by extension; raises ValueError on unsupported types."""

    def parse(self, file_path: str, file_type: str) -> str:
        ext = (file_type or "").lower()
        if not ext.startswith("."):
            ext = "." + ext
        fn = _EXT_TO_PARSER.get(ext)
        if fn is None:
            raise ValueError(f"不支持的文件类型: {ext}")
        return fn(file_path, ext)


default_parser = DefaultDocumentParser()
