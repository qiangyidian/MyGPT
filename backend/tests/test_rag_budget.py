"""Context 预算（RAG-09）的验收测试。

预算是两个独立限制的交点：**窗口是「能不能」**（超出就撑爆模型输入），**运维上限是
「付不付得起」**（``RAG_CONTEXT_TOKENS``）。这条测试集就是钉住「两者取小」，以及
拿不到窗口信息时必须有一条明确的退路而不是猜一个。
"""
from __future__ import annotations

import pytest

from app.rag.budget import (
    CONTEXT_BUDGET_RATIO,
    MIN_CONTEXT_BUDGET,
    compute_context_budget,
)


def test_unknown_window_falls_back_to_the_absolute_ceiling():
    b = compute_context_budget(context_window=None, absolute_fallback=6000)
    assert b.budget == 6000
    assert b.fallback_used is True
    assert b.context_window == 0


def test_a_known_window_takes_eighty_percent_of_what_is_left():
    b = compute_context_budget(
        context_window=10000,
        reserved_system=1000,
        reserved_query=500,
        reserved_output=1500,
    )
    assert b.available == 10000 - 3000
    assert b.budget == int(7000 * CONTEXT_BUDGET_RATIO)


def test_the_operator_ceiling_wins_on_a_large_window():
    """大窗口不能让上下文成本失控 —— 否则「模型窗口大」就等于「这轮可以贵十倍」。

    131k 窗口扣掉输出后约 100k 的可用预算，若没有运维上限就会全部喂给上下文，而这
    跟检索质量毫无关系。
    """
    b = compute_context_budget(
        context_window=131072, reserved_output=4096, hard_ceiling=6000
    )
    assert b.budget == 6000
    assert b.hard_ceiling == 6000
    assert b.fallback_used is False


def test_a_small_window_wins_over_a_generous_ceiling():
    """窗口约束仍然必需：小窗口模型上 6000 的上下文加上输出会直接撑爆输入。"""
    b = compute_context_budget(
        context_window=8192, reserved_output=4096, hard_ceiling=6000
    )
    assert b.budget < 6000
    assert b.budget == int((8192 - 4096) * CONTEXT_BUDGET_RATIO)


def test_a_ceiling_of_zero_means_no_ceiling():
    b = compute_context_budget(context_window=10000, hard_ceiling=0)
    assert b.budget == int(10000 * CONTEXT_BUDGET_RATIO)
    assert b.hard_ceiling == 0


def test_negative_reservations_are_clamped_to_zero():
    """负预留等于凭空放大窗口。"""
    b = compute_context_budget(context_window=1000, reserved_system=-500)
    assert b.reserved_system == 0
    assert b.budget == int(1000 * CONTEXT_BUDGET_RATIO)


def test_reservations_larger_than_the_window_leave_nothing():
    b = compute_context_budget(context_window=1000, reserved_output=5000)
    assert b.available == 0
    assert b.budget == 0


def test_a_uselessly_small_budget_falls_back_instead_of_emitting_half_a_chunk():
    """装不下一个分片的预算只会产出半句话的上下文，不如退到绝对兜底。"""
    tiny = int(MIN_CONTEXT_BUDGET / CONTEXT_BUDGET_RATIO) - 10
    b = compute_context_budget(
        context_window=tiny, reserved_output=0, absolute_fallback=6000
    )
    assert b.budget == 6000

    no_fallback = compute_context_budget(context_window=tiny, absolute_fallback=0)
    assert no_fallback.budget == 0


def test_the_ratio_is_clamped_to_a_sane_range():
    b = compute_context_budget(context_window=1000, ratio=5.0)
    assert b.budget == 1000  # ratio 会被夹到 1.0


def test_breakdown_is_serializable_and_explains_itself():
    meta = compute_context_budget(
        context_window=8192, reserved_system=100, reserved_output=1024, hard_ceiling=6000
    ).to_metadata()
    assert meta["context_window"] == 8192
    assert meta["reserved_output"] == 1024
    assert meta["context_budget"] == int((8192 - 1124) * CONTEXT_BUDGET_RATIO)
    assert meta["fallback_used"] is False


@pytest.mark.parametrize("window", [0, -1, None])
def test_every_flavour_of_unknown_window_takes_the_same_route(window):
    assert compute_context_budget(context_window=window, absolute_fallback=1234).budget == 1234
