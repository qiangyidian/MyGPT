"""评估语料体检 + 基线可计算（spec §12.3）。

这里**不**断言阈值：基线的作用是把数字摆到台面上，阈值在 S4 之后收紧。
"""
from __future__ import annotations

import pytest

from tests.memory.harness import CATEGORIES, load_cases, report_text, run


def test_corpus_is_well_formed():
    cases = load_cases()
    assert len(cases) >= 24, f"语料太少：{len(cases)}"
    for category in CATEGORIES:
        n = sum(1 for c in cases if c.category == category)
        assert n >= 4, f"{category} 只有 {n} 条，至少 4 条"
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids)), "用例 id 有重复"
    for case in cases:
        assert case.messages, f"{case.id} 没有消息"
        # attack 类可以以 assistant 收尾（at-assistant-side 专测助理侧内容不得成记忆）；
        # 但每个用例至少要有一条 user 消息——抽取器只吃最后一条 user 消息。
        assert any(m["role"] == "user" for m in case.messages), f"{case.id} 没有 user 消息"
        if case.category == "should_remember":
            assert case.expect_any, f"{case.id} 缺 expect_any"
        if case.category == "decoy":
            assert case.expect_none, f"{case.id} 缺 expect_none"


@pytest.mark.asyncio
async def test_baseline_report_is_computable(capsys):
    from app.agents.memory_auto_propose import extract_memory_candidates

    report = await run(load_cases(), extract_memory_candidates)
    with capsys.disabled():
        print(report_text(report))
    assert report.total >= 24
    assert 0.0 <= report.recall <= 1.0
    assert 0.0 <= report.false_positive <= 1.0
    assert report.per_category["attack"] >= 4
