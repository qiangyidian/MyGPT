"""积分的纯逻辑层：扣分公式与兑换码编解码。

刻意不碰数据库 —— 所有涉及会话、事务、行锁的部分在
:mod:`app.services.credit_service` 与 :mod:`app.services.redeem_service`。
这里只放可以脱离数据库单测的东西，与 :mod:`app.quotas` 的分层方式一致。

关于 :class:`CreditPolicy` 的存在理由：代码库既有的
:meth:`QuotaLimits.from_settings` / :func:`set_quota_service` 是一对
"从配置读 + 测试可注入" 的组合，积分沿用同一模式。测试需要验证拦截行为，
而 ``ENV=test`` 下 from_settings 会强制关闭 enforced，所以必须有一条
绕过配置的注入路径。
"""
from __future__ import annotations

import hashlib
import secrets
import unicodedata
from dataclasses import dataclass
from math import ceil
from typing import Any

from app.core.config import get_settings

# Crockford Base32：去掉 I / L / O / U。前三个是手抄歧义字符
# （1 和 l、0 和 O），U 去掉是为了避免生成出冒犯性单词。
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# 16 字符 × 5 bit = 80 bit 熵。配合兑换接口限流，枚举不可行。
CODE_LENGTH = 16
CODE_GROUP = 4
_PREFIX_LENGTH = 6

# 手抄容错映射：这些字符不在码表里，但用户很容易写错，归一化到码表内。
_CHAR_FIXES = {"I": "1", "L": "1", "O": "0"}


@dataclass(frozen=True)
class CreditPolicy:
    """积分策略。``enforced=False`` 时只记账不拦截（观察模式）。"""

    enforced: bool = False
    credits_per_usd: float = 1000.0
    credits_per_1k_tokens: float = 1.0
    signup_bonus: int = 0
    max_adjust: int = 10_000_000
    max_codes_per_batch: int = 5000

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> "CreditPolicy":
        s = settings or get_settings()
        # 与 QuotaLimits.from_settings 同款：测试环境强制关闭拦截，
        # 否则整个套件会在余额为 0 的种子用户上全线失败。
        enforced = bool(getattr(s, "CREDITS_ENFORCED", False)) and s.ENV != "test"
        return cls(
            enforced=enforced,
            credits_per_usd=float(getattr(s, "CREDITS_PER_USD", 1000.0)),
            credits_per_1k_tokens=float(
                getattr(s, "CREDITS_PER_1K_TOKENS_FALLBACK", 1.0)
            ),
            signup_bonus=max(0, int(getattr(s, "CREDITS_SIGNUP_BONUS", 0) or 0)),
            max_adjust=max(1, int(getattr(s, "CREDITS_MAX_ADJUST", 10_000_000))),
            max_codes_per_batch=max(
                1, int(getattr(s, "REDEEM_MAX_CODES_PER_BATCH", 5000))
            ),
        )


# --------------------------------------------------------------------------- #
# 扣分公式
# --------------------------------------------------------------------------- #
def compute_charge(
    cost_usd: float | None, total_tokens: int | None, policy: CreditPolicy
) -> int:
    """一轮对话该扣多少积分。

    优先用服务端实测成本换算。**成本未知或为 0 时必须回落到 token 计价** ——
    :func:`app.core.pricing.usage_cost` 对未配置定价的模型返回 ``None``
    （``app/core/pricing.py:75``），provider 也可能不报 ``cost_usd``。没有这条
    回落，这些模型的消耗完全免费。

    两个分支都 ``ceil`` 且下限为 1：真实产生过消耗的一轮，积分至少为 1。
    零消耗（mock 响应、无 usage 的失败轮）返回 0。
    """
    if cost_usd is not None and cost_usd > 0:
        return max(1, ceil(cost_usd * policy.credits_per_usd))
    tokens = int(total_tokens or 0)
    if tokens > 0:
        return max(1, ceil(tokens / 1000.0 * policy.credits_per_1k_tokens))
    return 0


# --------------------------------------------------------------------------- #
# 兑换码编解码
# --------------------------------------------------------------------------- #
def generate_code() -> str:
    """生成一个展示格式的兑换码（``XXXX-XXXX-XXXX-XXXX``）。

    用 :func:`secrets.choice` 而不是一次 ``token_bytes`` + base32 编码：前者
    逐个字符独立均匀取自码表，不存在"最后一组字节被截断导致熵不足"的问题。
    """
    chars = [secrets.choice(CROCKFORD_ALPHABET) for _ in range(CODE_LENGTH)]
    groups = [
        "".join(chars[i : i + CODE_GROUP])
        for i in range(0, CODE_LENGTH, CODE_GROUP)
    ]
    return "-".join(groups)


def normalize_code(raw: str) -> str:
    """把用户输入的任意格式归一化到码表内的紧凑形式。

    规则：NFKC 兼容归一化（全角字母/数字折叠回 ASCII）、去掉所有非字母数字
    字符、转大写、修正手抄歧义字符（I/L→1、O→0）。生成时与哈希前都必须走
    这条路径，两边一致才能匹配上。
    """
    folded = unicodedata.normalize("NFKC", raw or "")
    out: list[str] = []
    for ch in folded.upper():
        if not ch.isalnum():
            continue
        out.append(_CHAR_FIXES.get(ch, ch))
    return "".join(out)


def hash_code(normalized: str) -> str:
    """规范化后的码的 SHA-256 hex（64 字符）。

    库里只存哈希不存明文：兑换码是不记名凭证，等价于现金，明文入库意味着
    任何一次库泄露 / 备份外泄都等于直接漏钱。
    """
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def code_prefix(normalized: str) -> str:
    """前 6 字符明文前缀，仅供管理员在列表中辨认某张卡。"""
    return normalized[:_PREFIX_LENGTH]


# --------------------------------------------------------------------------- #
# 进程级策略单例 + 测试注入
# --------------------------------------------------------------------------- #
_credit_policy_singleton: CreditPolicy | None = None


def get_credit_policy() -> CreditPolicy:
    """返回进程级 :class:`CreditPolicy`，首次访问时从配置构建。"""
    global _credit_policy_singleton
    if _credit_policy_singleton is None:
        _credit_policy_singleton = CreditPolicy.from_settings()
    return _credit_policy_singleton


def set_credit_policy(policy: CreditPolicy | None) -> None:
    """测试注入：覆盖（``None`` 重置）进程级策略单例。"""
    global _credit_policy_singleton
    _credit_policy_singleton = policy


__all__ = [
    "CROCKFORD_ALPHABET",
    "CODE_LENGTH",
    "CreditPolicy",
    "code_prefix",
    "compute_charge",
    "generate_code",
    "get_credit_policy",
    "hash_code",
    "normalize_code",
    "set_credit_policy",
]