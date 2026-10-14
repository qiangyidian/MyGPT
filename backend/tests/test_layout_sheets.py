"""表格的逐行字符区间：让 chunk 能落到具体行上。"""
from __future__ import annotations

from app.rag.layout import page_bases
from app.rag.locator import locate
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


def test_xlsx_layout_rows_cover_every_data_line(tmp_path):
    parsed = default_parser.parse(_make_xlsx(tmp_path / "a.xlsx"), ".xlsx")

    assert parsed.layout is not None
    assert len(parsed.layout.sheets) == 1
    sheet = parsed.layout.sheets[0]
    assert sheet.name == "Sheet1"
    # 块首那行 "## Sheet: Sheet1" 占了第 0 行，表头因此在第 1 行。
    assert sheet.header_line == 1
    assert len(sheet.rows) == 5

    lines = parsed.text.splitlines()
    for row in sheet.rows:
        assert parsed.text[row.char_start : row.char_end] == lines[sheet.header_line + 1 + row.row]
        # 数据行必须真的在表头之后
        assert row.char_start > 0


def test_xlsx_layout_text_is_unchanged(tmp_path):
    """加布局不得改变 text —— text 变了就是隐性重索引。"""
    src = _make_xlsx(tmp_path / "b.xlsx")
    parsed = default_parser.parse(src, ".xlsx")
    assert "## Sheet: Sheet1" in parsed.text
    assert "序号 | 条款 | 要求" in parsed.text
    assert "5 | 8.5.1-4 | 要求内容 4" in parsed.text


def test_csv_layout_uses_line_zero_as_header(tmp_path):
    """csv 没有 sheet 标题行，表头就在第 0 行 —— 写死 1 会整表错位。"""
    src = tmp_path / "c.csv"
    src.write_text("序号,条款\n1,8.5.1\n2,8.5.2\n", encoding="utf-8")
    parsed = default_parser.parse(str(src), ".csv")

    assert parsed.layout is not None
    sheet = parsed.layout.sheets[0]
    assert sheet.header_line == 0
    assert len(sheet.rows) == 2
    # 端到端：第一行数据应当定位到第一行数据，而不是表头
    loc = locate(
        parsed,
        page=None,
        char_start=sheet.rows[0].char_start,
        char_end=sheet.rows[0].char_end,
    )
    assert loc is not None and loc.kind == "sheet_range"
    assert loc.header == ("序号", "条款")
    assert loc.highlight == (0, 1)
    assert loc.rows[0][1] == "8.5.1"


def test_page_bases_skips_empty_pages():
    assert page_bases(["aa", "", "bbb"]) == {1: 0, 3: 4}  # "aa" + "\n\n" + "bbb"
