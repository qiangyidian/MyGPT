"""RAG-06 查询理解的验收测试。

这一阶段的全部价值在于「多做可解释的抽取，少做不能确定的推测」。所以测试也分两组：
抽得出来的一律钉死具体值；抽不出来/不该应用的必须**留下原因**，而不是静默丢掉。
"""
from __future__ import annotations

from datetime import date

import pytest

from app.rag.query_plan import (
    FILTER_SCHEMA,
    build_query_plan,
    detect_language,
    resolve_relative_dates,
)

TODAY = date(2026, 10, 13)


# --------------------------------------------------------------------------- #
# 日期
# --------------------------------------------------------------------------- #
def test_iso_month_expands_to_a_closed_interval():
    hits, normalized = resolve_relative_dates("2025-03 的故障", today=TODAY)
    assert [r.as_filter() for _, _, r in hits] == [{"from": "2025-03-01", "to": "2025-03-31"}]
    assert "2025-03-01_2025-03-31" in normalized


def test_iso_range_keeps_both_ends_inclusive():
    hits, _ = resolve_relative_dates("2025-01-01 至 2025-12-31", today=TODAY)
    assert len(hits) == 1
    assert hits[0][2].start == date(2025, 1, 1)
    assert hits[0][2].end == date(2025, 12, 31)


def test_february_clamps_instead_of_overflowing():
    """「上个月」落在 3 月时，区间末端必须是 2/28 而不是 3/3。"""
    hits, _ = resolve_relative_dates("上个月", today=date(2026, 3, 31))
    assert [r.as_filter() for _, _, r in hits] == [{"from": "2026-02-01", "to": "2026-02-28"}]


def test_last_year_normalizes_to_a_concrete_year():
    """文档的例子：``去年`` 要归一化成具体年份，而不是原样保留。"""
    plan = build_query_plan("去年服务器故障的原因是什么？", today=TODAY)
    assert "2025-01-01_2025-12-31" in plan.normalized_query
    assert "去年" not in plan.normalized_query


def test_a_shorter_pattern_does_not_eat_an_already_claimed_span():
    """``2025-03-01`` 同时能被 ISO 日期和 ISO 月份匹配；只允许命中一次。"""
    hits, normalized = resolve_relative_dates("2025-03-01 那天", today=TODAY)
    assert len(hits) == 1
    assert hits[0][2].start == hits[0][2].end == date(2025, 3, 1)
    assert normalized.startswith("2025-03-01")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("今天", (TODAY, TODAY)),
        ("昨天", (date(2026, 10, 12), date(2026, 10, 12))),
        ("前天", (date(2026, 10, 11), date(2026, 10, 11))),
        ("本周", (date(2026, 10, 12), date(2026, 10, 18))),  # 周一为一周之始
        ("上周", (date(2026, 10, 5), date(2026, 10, 11))),
        ("过去 7 天", (date(2026, 10, 7), TODAY)),
        ("最近3个月", (date(2026, 7, 13), TODAY)),
        ("前 2 天", (date(2026, 10, 11), date(2026, 10, 11))),
        ("本季度", (date(2026, 10, 1), date(2026, 12, 31))),
    ],
)
def test_relative_phrases_resolve_as_closed_intervals(text, expected):
    hits, _ = resolve_relative_dates(text, today=TODAY)
    assert hits, f"{text!r} 没有被解析"
    assert (hits[0][2].start, hits[0][2].end) == expected


def test_an_invalid_calendar_date_is_not_a_hit():
    hits, normalized = resolve_relative_dates("2025-13-45 的记录", today=TODAY)
    assert hits == []
    assert normalized == "2025-13-45 的记录"


# --------------------------------------------------------------------------- #
# 实体
# --------------------------------------------------------------------------- #
def test_a_prefixed_code_is_an_error_code():
    plan = build_query_plan("E1001 是什么意思？", today=TODAY)
    assert [(e.type, e.value) for e in plan.entities] == [("error_code", "E1001")]


def test_bare_digits_are_not_codes():
    """「第 3 章」「3 个步骤」不能被当成编号 —— 误判成本远高于漏判。"""
    plan = build_query_plan("第 3 章讲了 3 个步骤", today=TODAY)
    assert plan.entities == []
    assert plan.requires_exact_match is False


def test_a_registered_model_wins_over_the_error_code_heuristic():
    plan = build_query_plan("E1001 的参数", today=TODAY, model_lexicon=frozenset({"E1001"}))
    assert [e.type for e in plan.entities] == ["product_model"]


def test_quoted_phrases_are_entities():
    plan = build_query_plan("「跨域访问」怎么配置", today=TODAY)
    assert [(e.type, e.value) for e in plan.entities] == [("quoted_phrase", "跨域访问")]


@pytest.mark.parametrize(
    "question,intent", [
        ("E1001 是什么", "lookup"),
        ("为什么服务会超时", "cause_analysis"),
        ("方案 A 和方案 B 有什么区别", "compare"),
        ("你觉得明年会怎样", "no_answer_candidate"),
        ("部署流程", "explain"),
    ],
)
def test_intent_is_classified_deterministically(question, intent):
    assert build_query_plan(question, today=TODAY).intent == intent


@pytest.mark.parametrize(
    "text,language",
    [("服务器故障", "zh-CN"), ("server failure", "en"), ("KB 的 chunk 大小", "mixed"), ("", "unknown")],
)
def test_language_detection(text, language):
    assert detect_language(text) == language


# --------------------------------------------------------------------------- #
# 过滤条件：只有能下推的才应用，其余必须留痕
# --------------------------------------------------------------------------- #
def test_document_ids_from_the_caller_become_a_pushed_down_filter():
    plan = build_query_plan("问题", today=TODAY, document_ids=["d-1", "d-2"])
    assert plan.filter_map() == {"document_id": ["d-1", "d-2"]}
    assert plan.rejected_filters == []


def test_a_date_filter_is_recorded_as_not_indexed_rather_than_applied():
    """``updated_at`` 在白名单里但索引层查不了：不应用，并说明原因。

    记成 ``field_not_in_schema`` 会误导运维去加 schema 字段；真实动作是给那一列建索引。
    """
    plan = build_query_plan("去年的事故", today=TODAY)
    assert plan.filter_map() == {}
    reasons = {r.reason for r in plan.rejected_filters}
    assert reasons == {"field_not_indexed"}
    assert {r.field for r in plan.rejected_filters} == {"updated_at"}
    # 拒绝必须带触发原文，否则人工归因时无从对应。
    assert plan.rejected_filters[0].raw_text == "去年"


def test_every_whitelisted_field_declares_its_index_status():
    """白名单是一个契约：字段与「能不能下推」必须同时声明。"""
    for field, spec in FILTER_SCHEMA.items():
        assert isinstance(field, str) and field
        assert spec.ops, f"{field} 没有声明任何允许的操作符"
        assert set(spec.ops) <= {"eq", "in", "gt", "gte", "lt", "lte", "between"}


def test_an_exact_code_query_demands_the_keyword_channel():
    """纯编号查询必须有关键词通道：语义向量找 ``E1001`` 本来就不可靠。"""
    assert build_query_plan("E1001 代表什么", today=TODAY).requires_exact_match is True
    assert build_query_plan("服务为什么慢", today=TODAY).requires_exact_match is False


def test_the_original_query_is_always_preserved_verbatim():
    raw = "去年  E1001  的问题？"
    plan = build_query_plan(raw, today=TODAY)
    assert plan.original_query == raw
    assert plan.normalized_query != raw


def test_variants_stay_empty_because_rewriting_is_not_calibrated_yet():
    plan = build_query_plan("服务器为什么慢", today=TODAY)
    assert plan.query_variants == []
    assert plan.plan_version == "query-plan-v1"


def test_an_empty_question_plans_without_crashing():
    plan = build_query_plan("", today=TODAY)
    assert plan.original_query == ""
    assert plan.entities == [] and plan.filters == []
