"""风险分级与 PII 扫描的验收测试（RAG-13 的准入判定）。

这个分类器决定「答案缓冲还是流式」，是整条链路上唯一会改变用户可见行为的分支，因此
它必须**宁可误杀**：文档明写「分类器不确定时按高风险处理」。这里同时钉住词表的维护
约定（冻结常量 + 配置只能追加），因为那是文档 5.2 的评审要求，靠约定守不住。
"""
from __future__ import annotations

import pytest

from app.rag import risk as risk_mod
from app.rag.risk import (
    HIGH_RISK_TERMS,
    RISK_CATEGORIES,
    classify_risk,
    extra_terms_from_settings,
    risk_policy_version,
    scan_pii,
)


class _Settings:
    def __init__(self, extra: str = "") -> None:
        self.RAG_HIGH_RISK_EXTRA_TERMS = extra


# --------------------------------------------------------------------------- #
# 七类
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "question,category",
    [
        ("这份合同的违约金条款有法律风险吗", "legal_compliance"),
        ("今年的调薪方案怎么定", "hr_compensation"),
        ("这笔付款需要谁审批", "financial_transaction"),
        ("怎么查别人的身份证号", "personal_sensitive_info"),
        ("给张三点权限授予需要什么流程", "access_control"),
        ("这次生产变更的回滚方案是什么", "production_change"),
        ("数据泄露的安全事件怎么应急处置", "security_incident"),
    ],
)
def test_each_documented_high_risk_category_is_detected(question, category):
    assessment = classify_risk(question, extra_terms={})
    assert assessment.is_high_risk is True
    assert category in assessment.categories


def test_a_plain_product_question_is_not_high_risk():
    assessment = classify_risk("知识库怎么上传文档", extra_terms={})
    assert assessment.is_high_risk is False
    assert assessment.categories == ()
    assert assessment.certainty == "none"


def test_an_empty_question_is_not_high_risk():
    assert classify_risk("", extra_terms={}).is_high_risk is False


def test_a_weak_term_alone_still_counts_as_high_risk():
    """「不确定时按高风险处理」的落地：只有弱词命中同样判高风险。

    「合规」不在强词表里（强词是「合规审查」「合规风险」），但它照样把问题推进缓冲
    通道 —— 宁可让一批普通问题多花一次缓冲，也不让一条合规意见溜进流式通道。
    """
    assessment = classify_risk("这个做法符合合规要求吗", extra_terms={})
    assert assessment.is_high_risk is True
    assert assessment.certainty == "weak"
    assert "合规" in assessment.matched_terms


def test_a_strong_term_upgrades_certainty():
    assessment = classify_risk("这次合规审查需要哪些材料", extra_terms={})
    assert assessment.certainty == "strong"


def test_all_matching_categories_are_reported_not_just_one():
    """不选「主类别」：选一个需要一套优先级，而那套优先级只会成为争论来源。"""
    assessment = classify_risk("这次付款和权限授予怎么走合规流程", extra_terms={})
    assert set(assessment.categories) >= {
        "legal_compliance",
        "financial_transaction",
        "access_control",
    }


def test_matched_terms_are_deduplicated_and_ordered():
    assessment = classify_risk("权限 权限 权限授予", extra_terms={})
    assert len(assessment.matched_terms) == len(set(assessment.matched_terms))


def test_metadata_is_serializable_and_carries_the_policy_version():
    meta = classify_risk("调薪方案", extra_terms={}).to_metadata()
    assert meta["is_high_risk"] is True
    assert meta["policy_version"] == risk_mod.RISK_POLICY_VERSION
    assert isinstance(meta["categories"], list)


# --------------------------------------------------------------------------- #
# 词表维护约定（文档 5.2 的评审要求）
# --------------------------------------------------------------------------- #
def test_every_documented_category_has_a_lexicon():
    assert set(RISK_CATEGORIES) == set(HIGH_RISK_TERMS)
    for category, (strong, weak) in HIGH_RISK_TERMS.items():
        assert strong, f"{category} 没有强词，强判定永远不会触发"
        assert weak, f"{category} 没有弱词，不确定兜底永远不会触发"


def test_config_can_only_add_terms_never_remove_them():
    """配置能加，不能减 —— 删词必须改代码走评审。"""
    added = classify_risk("堡垒机怎么用", extra_terms={"access_control": ("堡垒机",)})
    assert added.is_high_risk is True

    # 配置里没有「删除」这一说：传空配置不会让默认词失效。
    baseline = classify_risk("调薪方案", extra_terms={})
    assert baseline.is_high_risk is True


def test_extra_terms_parser_ignores_junk_but_keeps_valid_entries():
    parsed = extra_terms_from_settings(
        _Settings("legal_compliance:跨境数据传输, not-a-category:x, access_control:堡垒机, broken")
    )
    assert parsed == {
        "legal_compliance": ("跨境数据传输",),
        "access_control": ("堡垒机",),
    }


def test_policy_version_changes_when_config_adds_terms():
    """追加了配置词却报同一个版本号，等于把配置差异藏起来。"""
    assert risk_policy_version(_Settings("")) == risk_mod.RISK_POLICY_VERSION
    assert risk_policy_version(_Settings("access_control:堡垒机")) != risk_mod.RISK_POLICY_VERSION


# --------------------------------------------------------------------------- #
# PII
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,kind",
    [
        ("身份证 110101199003071234", "cn_id_card"),
        ("卡号 6222021234567890123", "bank_card"),
        ("手机 13800138000", "cn_mobile"),
        ("邮箱 a.b@example.com", "email"),
    ],
)
def test_pii_patterns_are_detected(text, kind):
    assert kind in scan_pii(text)


def test_clean_text_has_no_pii():
    assert scan_pii("错误码 E1001 表示网络不可达，巡检周期 7 天。") == []


def test_pii_scan_reports_kinds_not_values():
    """只报种类，不回传命中值 —— 命中值本身就是敏感数据。"""
    found = scan_pii("身份证 110101199003071234")
    assert found == ["cn_id_card"]
    assert all("1101" not in item for item in found)
