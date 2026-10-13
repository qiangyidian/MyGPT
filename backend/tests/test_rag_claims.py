"""claim 切分与归属的验收测试（RAG-12 的 claim 级映射）。

这一层最容易出的错是**切坏**：多切一刀会把半句话摘掉，摘错位置会让用户看到的答案和
校验结论对不上。所以测试集中在三件事：不该切的地方没切、该切的地方切了、摘除按坐标
而不是按字符串。
"""
from __future__ import annotations

from itertools import pairwise

from app.rag.claims import (
    build_claims,
    cited_source_indexes,
    claim_source_indexes,
    remove_claim_spans,
    split_claim_spans,
    split_claims,
    uncited_claim_ratio,
)
from app.rag.contracts import ContextItem

SOURCE_INDEX = {
    1: ContextItem(context_id="ctx-1", chunk_id="c1", text="E1001 表示网络不可达。"),
    2: ContextItem(context_id="ctx-2", chunk_id="c2", text="巡检周期为 7 天。"),
}


def _texts(answer: str) -> list[str]:
    return [answer[s:e] for s, e in split_claim_spans(answer)]


# --------------------------------------------------------------------------- #
# 不该切的地方
# --------------------------------------------------------------------------- #
def test_decimal_and_version_dots_do_not_split():
    """``3.14`` / ``v1.2.3`` 里切一刀会造出两条互不成立的主张。"""
    assert _texts("耗时 3.14 秒。") == ["耗时 3.14 秒。"]
    assert _texts("升级到 v1.2.3 即可。") == ["升级到 v1.2.3 即可。"]


def test_semicolons_and_commas_do_not_split_chinese():
    """``；`` ``，`` 是同一句内部的停顿，不是句末。"""
    assert _texts("先重启；再观察，最后确认。") == ["先重启；再观察，最后确认。"]


def test_fenced_code_is_never_a_claim():
    answer = "说明如下：\n\n```python\nx = 1. \nreturn x\n```\n\n以上。"
    texts = _texts(answer)
    assert "x = 1." not in texts
    assert "```" not in "".join(texts)


def test_table_rows_and_list_items_stay_whole():
    answer = "| 错误码 | 原因 |\n| --- | --- |\n| E1001 | 网络不可达 |\n- 第一项，说明很长。\n- 第二项"
    texts = _texts(answer)
    assert "| E1001 | 网络不可达 |" in texts
    assert "第一项，说明很长。" in texts and "第二项" in texts


def test_headings_and_list_markers_are_stripped_from_the_claim_text():
    assert _texts("## 故障处理\n- 检查网线") == ["故障处理", "检查网线"]


def test_closing_quotes_are_absorbed_into_the_sentence():
    """句末标点后面的收尾引号不能留成孤儿。"""
    assert _texts("他说「不支持。」然后走了") == ["他说「不支持。」", "然后走了"]


# --------------------------------------------------------------------------- #
# 坐标
# --------------------------------------------------------------------------- #
def test_spans_point_at_the_real_substring():
    answer = "第一句。第二句很长，带 E1001。第三句。"
    for start, end in split_claim_spans(answer):
        assert answer[start:end] in answer
        assert answer[start:end] == answer[start:end].strip()


def test_spans_do_not_overlap_and_are_ordered():
    answer = "A 句。B 句。\nC 句。"
    spans = split_claim_spans(answer)
    assert spans == sorted(spans)
    for (_, prev_end), (next_start, _) in pairwise(spans):
        assert prev_end <= next_start


# --------------------------------------------------------------------------- #
# 归属
# --------------------------------------------------------------------------- #
def test_a_citation_inside_a_sentence_maps_to_its_context_id():
    claims = build_claims("E1001 表示网络不可达 [source 1]。", SOURCE_INDEX)
    assert len(claims) == 1
    assert claims[0].evidence_context_ids == ["ctx-1"]


def test_a_citation_the_reader_never_gave_is_not_evidence():
    """``[source 9]`` 不在本次请求的序号表里 → 不产生证据。这是引用幻觉的判定依据。"""
    claims = build_claims("这是编的 [source 9]。", SOURCE_INDEX)
    assert claims[0].evidence_context_ids == []
    assert claims[0].text == "这是编的 [source 9]。"


def test_claims_without_any_marker_have_no_evidence():
    claims = build_claims("这是一句没有任何引用的话。", SOURCE_INDEX)
    assert claims[0].evidence_context_ids == []


def test_multiple_markers_in_one_sentence_all_map():
    claims = build_claims("两条都对 [source 1][source 2]。", SOURCE_INDEX)
    assert claims[0].evidence_context_ids == ["ctx-1", "ctx-2"]


def test_claim_ids_are_stable_and_one_based():
    claims = build_claims("一。二。三。", SOURCE_INDEX)
    assert [c.claim_id for c in claims] == ["claim-1", "claim-2", "claim-3"]


def test_chinese_source_marker_variant_is_recognized():
    assert claim_source_indexes("见 [来源 2]") == [2]
    assert claim_source_indexes("见 [来源：2]") == [2]


# --------------------------------------------------------------------------- #
# 摘除
# --------------------------------------------------------------------------- #
def test_removing_a_claimed_span_takes_exactly_that_sentence():
    answer = "第一句保留。第二句要删。第三句保留。"
    claims = build_claims(answer, SOURCE_INDEX)
    claims[1].dropped = True
    assert remove_claim_spans(answer, claims) == "第一句保留。第三句保留。"


def test_removal_handles_a_repeated_sentence_without_deleting_the_wrong_one():
    """按字符串替换会删错位置；按坐标才删得准。"""
    answer = "重复句。中间句。重复句。"
    claims = build_claims(answer, SOURCE_INDEX)
    claims[2].dropped = True
    assert remove_claim_spans(answer, claims) == "重复句。中间句。"


def test_removal_keeps_markdown_structure():
    """不重建 markdown：标题、列表符号必须原样留着。"""
    answer = "## 标题\n\n- 保留项\n- 要删项\n"
    claims = build_claims(answer, SOURCE_INDEX)
    target = next(c for c in claims if c.text == "要删项")
    target.dropped = True
    out = remove_claim_spans(answer, claims)
    assert "## 标题" in out
    assert "- 保留项" in out
    assert "要删项" not in out


def test_removal_of_everything_yields_empty_text():
    answer = "只有一句。"
    claims = build_claims(answer, SOURCE_INDEX)
    claims[0].dropped = True
    assert remove_claim_spans(answer, claims) == ""


# --------------------------------------------------------------------------- #
# 指标
# --------------------------------------------------------------------------- #
def test_cited_indexes_only_count_surviving_claims():
    claims = build_claims("甲 [source 1]。乙 [source 2]。", SOURCE_INDEX)
    claims[1].dropped = True
    assert cited_source_indexes(claims) == [1]


def test_uncited_ratio_is_the_documented_metric():
    claims = build_claims("有引用 [source 1]。没引用。", SOURCE_INDEX)
    assert uncited_claim_ratio(claims) == 0.5


def test_uncited_ratio_of_no_claims_is_zero():
    assert uncited_claim_ratio([]) == 0.0


def test_split_claims_matches_the_span_helper():
    answer = "甲。乙。"
    assert split_claims(answer) == [answer[s:e] for s, e in split_claim_spans(answer)]
