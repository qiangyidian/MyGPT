"""RAG-13 的前置半步：判断这一问是不是文档定义的**高风险类别**。

设计文档第 1.3 节把七类问题钉死为高风险：法律/合规意见、薪酬/人事决定、财务交易/
付款、个人敏感信息、权限授予/撤销、生产环境变更、安全事件处置。对这些问题的答案
要求「强制引用验证和 ACL 重验；无法确认引用支持时拒答或转人工」。这条规则的实际
后果是 RAG-13 的**执行位置**：高风险回合必须缓冲后校验（否则拒答没有意义，用户已经
逐字看完了），普通回合才能继续流式。

所以这个分类器是「流不流式」的唯一开关，它必须**宁可误杀**：「分类器不确定时按高
风险处理」（文档原文）。落地方式是按类别维护强弱两级词表——

  * strong 命中 → 直接判高风险；
  * 只有 weak 命中（例如只有「合规」而没有「法律」/「监管」）→ **也判高风险**。
    这就是「不确定按高风险处理」的具体含义：宁可让一批普通问题多花一次缓冲，也不
    让一条合规意见溜进流式通道。

**词表维护约定**（文档 5.2：安全负责人可增加类别，不可未经评审删除默认类别）：

  * ``HIGH_RISK_TERMS`` 是**冻结常量**。要靠改代码 + 评审才能删词，这是刻意的摩擦。
  * ``RAG_HIGH_RISK_EXTRA_TERMS`` 配置项只允许**追加**，且追加进的是 weak 级——
    配置改不动删减，也改不动强判定的语义。

这两条各有一个单测钉着（``test_risk.py``），免得日后有人「顺手」把词表挪进配置。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "HIGH_RISK_TERMS",
    "RISK_CATEGORIES",
    "RiskAssessment",
    "classify_risk",
    "extra_terms_from_settings",
    "risk_policy_version",
]

RISK_POLICY_VERSION = "risk-v1"

#: 七类的规范名 —— 与文档 1.3 / 4.2 的列举一一对应，顺序也一致。
RISK_CATEGORIES: tuple[str, ...] = (
    "legal_compliance",
    "hr_compensation",
    "financial_transaction",
    "personal_sensitive_info",
    "access_control",
    "production_change",
    "security_incident",
)

#: 每类的 (strong, weak) 词表。strong = 命中即无歧义；weak = 命中即「疑似」，
#: 按文档要求同样按高风险处理。弱词放的是同域但更常见的说法，正是容易漏掉的那批。
HIGH_RISK_TERMS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "legal_compliance": (
        ("法律意见", "法律风险", "合规审查", "合规风险", "违法", "违规", "法务", "诉讼",
         "合同条款", "违约金", "知识产权", "侵权", "监管要求", "法律依据", "条例", "判例"),
        ("合规", "法规", "监管", "政策要求", "条款", "免责", "责任划分"),
    ),
    "hr_compensation": (
        ("薪酬", "工资", "薪资", "绩效奖金", "年终奖", "调薪", "晋升", "辞退", "解雇",
         "劳动仲裁", "竞业限制", "股权激励", "社保基数", "人员编制"),
        ("人事", "招聘标准", "考核", "职级", "福利", "离职", "加班费"),
    ),
    "financial_transaction": (
        ("付款", "转账", "报销", "发票", "对账", "应收", "应付", "预算审批", "资金",
         "结算金额", "税率", "开票", "打款"),
        ("财务", "成本核算", "费用", "利润", "账期", "收款"),
    ),
    "personal_sensitive_info": (
        ("身份证号", "身份证", "银行卡号", "账号密码", "社保号", "护照号", "手机号",
         "住址", "病历", "健康状况", "体检报告", "生物特征", "人脸数据", "个人隐私"),
        ("个人信息", "隐私", "敏感信息", "联系方式", "健康"),
    ),
    "access_control": (
        ("权限授予", "授权", "撤权", "取消权限", "开通权限", "管理员权限", "角色变更",
         "访问控制", "越权", "提权", "账号权限"),
        ("权限", "访问范围", "可见范围", "角色"),
    ),
    "production_change": (
        ("生产变更", "上线", "发布到生产", "生产环境", "回滚", "停机", "扩容", "缩容",
         "数据库迁移", "变更窗口", "灰度发布"),
        ("部署", "配置变更", "发布", "运维操作"),
    ),
    "security_incident": (
        ("安全事件", "安全漏洞", "数据泄露", "入侵", "攻击", "勒索", "木马", "应急响应",
         "安全告警", "渗透测试", "凭据泄露", "应急演练"),
        ("安全", "漏洞", "告警", "排查风险"),
    ),
}


@dataclass(frozen=True)
class RiskAssessment:
    """一次风险判定的完整留痕。

    ``categories`` 记录**全部**命中类别而不是只留一个：选一个「主类别」需要一套
    优先级，而那套优先级只会成为日后争论的来源，且对策略没有任何影响（高风险就是
    高风险，走同一条缓冲通道）。
    """

    is_high_risk: bool
    categories: tuple[str, ...] = ()
    matched_terms: tuple[str, ...] = ()
    #: 分级命中的最强证据等级：命中任一 strong 即 ``strong``，仅 weak 即 ``weak``。
    certainty: str = "none"
    classifier_version: str = RISK_POLICY_VERSION
    #: 供 trace 使用的明细：类别 → 命中的词。
    hits: dict[str, list[str]] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, Any]:
        """进 message metadata / trace 的精简形式（不放大段原文）。"""
        return {
            "policy_version": self.classifier_version,
            "is_high_risk": self.is_high_risk,
            "categories": list(self.categories),
            "certainty": self.certainty,
            "matched_terms": list(self.matched_terms),
        }


def extra_terms_from_settings(settings: Any = None) -> dict[str, tuple[str, ...]]:
    """从 ``RAG_HIGH_RISK_EXTRA_TERMS`` 读追加词，形如 ``legal_compliance:跨境数据传输,access_control:堡垒机``。

    只返回**追加**项。解析失败的条目静默跳过（错的配置不该让一整轮对话失败），
    但返回的字典天然只能加、不能减 —— 见本模块 docstring 的维护约定。
    """
    from app.core.config import get_settings

    raw = str(getattr(settings or get_settings(), "RAG_HIGH_RISK_EXTRA_TERMS", "") or "")
    out: dict[str, list[str]] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        category, term = chunk.split(":", 1)
        category, term = category.strip(), term.strip()
        if category not in RISK_CATEGORIES or not term:
            continue
        out.setdefault(category, []).append(term)
    return {k: tuple(v) for k, v in out.items()}


def risk_policy_version(settings: Any = None) -> str:
    """生效的策略版本。追加了配置词就带后缀，否则同一个版本号会掩盖配置差异。"""
    extra = extra_terms_from_settings(settings)
    if not extra:
        return RISK_POLICY_VERSION
    digest = ",".join(sorted(f"{k}:{len(v)}" for k, v in extra.items()))
    return f"{RISK_POLICY_VERSION}+{digest}"


def _matches(text_lower: str, terms: tuple[str, ...]) -> list[str]:
    return [t for t in terms if t.lower() in text_lower]


def classify_risk(question: str, *, extra_terms: dict[str, tuple[str, ...]] | None = None) -> RiskAssessment:
    """判定一个问题是否属于七类高风险。

    只看**用户问题**，不看检索结果或答案——因为它的用途是在生成**之前**决定走流式
    还是缓冲，那时候还没有答案可看。
    """
    text = (question or "").lower()
    if not text.strip():
        return RiskAssessment(is_high_risk=False, classifier_version=risk_policy_version())

    extra = extra_terms or {}
    categories: list[str] = []
    matched: list[str] = []
    hits: dict[str, list[str]] = {}
    strongest = "none"

    for category in RISK_CATEGORIES:
        strong, weak = HIGH_RISK_TERMS[category]
        hit_strong = _matches(text, strong)
        hit_weak = _matches(text, weak) + _matches(text, extra.get(category, ()))
        if not (hit_strong or hit_weak):
            continue
        categories.append(category)
        found = hit_strong + hit_weak
        hits[category] = found
        matched.extend(found)
        if hit_strong:
            strongest = "strong"
        elif strongest == "none":
            strongest = "weak"

    return RiskAssessment(
        # 只有 weak 命中同样算高风险：这就是「不确定时按高风险处理」。
        is_high_risk=bool(categories),
        categories=tuple(categories),
        # 去重但保持出现顺序，便于人工核对「是哪几个词触发的」。
        matched_terms=tuple(dict.fromkeys(matched)),
        certainty=strongest,
        classifier_version=risk_policy_version(),
        hits=hits,
    )


# --------------------------------------------------------------------------- #
# PII：与「问题是否高风险」不同 —— PII 扫的是**答案文本**，属于 RAG-13 的
# personal_sensitive_info 检查。放在这里是因为两者的规则版本与维护人相同。
#
# **顺序即优先级**，从最具体到最宽泛：``scan_pii`` 会让先命中的模式占用字符区间，
# 后面的模式看不到它。身份证必须排在银行卡之前 —— 18 位身份证本身也满足 16–19 位
# 数字的形状。
# --------------------------------------------------------------------------- #
PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 中国大陆身份证：18 位（末位可为 X）。前 6 位地址码不做校验，只做形状匹配。
    ("cn_id_card", re.compile(r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")),
    # 银行卡：16–19 位连续数字，且不与身份证/长数字串相邻。Luhn 校验会误杀测试卡号，
    # 这里只做长度与边界匹配。
    ("bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    # 中国大陆手机号：1 开头 11 位。
    ("cn_mobile", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    # 邮箱。
    ("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
)


def scan_pii(text: str) -> list[str]:
    """返回命中的 PII 种类（去重，按模式的具体程度排序）。**只报种类，不回传命中值**。

    模式之间会**重叠**：18 位身份证同时满足 16–19 位银行卡的形状。所以按「具体 →
    宽泛」的顺序扫描，并且已命中的字符区间用空格占位后再扫下一个模式 —— 否则一个
    身份证会被同时报成身份证和银行卡。今天两者都是 PII、都会被拦下，所以这看起来只是
    一条报错信息；但策略一旦对两类区别对待（例如银行卡需脱敏、身份证需拒答），错的
    种类就会导致错的动作。
    """
    value = text or ""
    found: list[str] = []
    masked = value
    for name, pattern in PII_PATTERNS:
        match = pattern.search(masked)
        if match is None:
            continue
        found.append(name)
        # 占位而不是删除：删掉会让右侧字符左移，可能拼出新的假匹配。
        masked = pattern.sub(lambda m: " " * len(m.group(0)), masked)
    return found
