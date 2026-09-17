# 积分与兑换码 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 平台接入预付费积分 —— 管理员生成兑换码，用户输入兑换码获得积分，积分按服务端实测的模型成本逐轮扣减，余额不足时拦截。

**Architecture:** 账户行（`credit_accounts`，权威余额）+ 追加式账本（`credit_ledger`，审计与幂等来源），所有余额变动在同一 DB 事务内完成。三个不变量各由数据库机制兜底：兑换码用单语句 CAS 防重复兑换、扣费用唯一部分索引防重复扣减、余额用 `SELECT ... FOR UPDATE` 防并发写坏。积分链路挂在既有 `_charge_quota_if_enabled` 计费接缝旁，覆盖内联流式与 durable worker 两条执行路径。

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async + Alembic + PostgreSQL（测试用 aiosqlite）+ Next.js 15 App Router + React Query + Tailwind + shadcn/ui + pytest + vitest

**Spec:** `docs/superpowers/specs/2026-09-17-credits-and-redeem-codes-design.md`

## Global Constraints

- 分支：`feat/credits-redeem-codes`（已创建，spec 已提交为 `f2b30bd`）
- 后端测试运行目录必须是 `backend/`；命令 `python -m pytest tests/<file> -v`
- 前端测试命令在 `frontend/` 下：`npx vitest run <file>`
- 测试环境 `ENV=test`：数据库是内存 SQLite（`tests/conftest.py` 强制），配额与限流默认禁用
- 所有对外错误必须是 `AppException(status_code, code, message, extra?)`（`app/core/exceptions.py:21`），不要用裸 `HTTPException` —— 只有 `AppException` 会发出前端能解析的 `code` 字段
- 金额/积分为 `BIGINT`，Python 侧一律 `int`，禁止 float
- 时间戳一律 `datetime.now(UTC)`（Python 侧），不要用 `func.now()` 写业务值
- 中文注释与中文用户文案，与代码库现有风格一致
- 不得修改 `app/quotas.py` 的既有语义；积分是独立叠加的一层
- 前端不得新增 zustand store（避免既有 selector 陷阱），状态一律走 React Query
- 每个 Task 结束必须提交，提交信息用中文，遵循 `feat(credits): ...` / `fix(credits): ...` / `test(credits): ...` 格式

---

### Task 1: 纯逻辑层 —— 扣分公式与兑换码编解码

**Files:**
- Create: `backend/app/credits.py`
- Modify: `backend/app/core/config.py`（在 `QUOTA_RUN_TTL_SECONDS` 之后追加）
- Modify: `backend/.env.example`
- Test: `backend/tests/test_credits.py`

**Interfaces:**
- Consumes: `app.core.config.get_settings`
- Produces:
  - `@dataclass(frozen=True) CreditPolicy(enforced: bool, credits_per_usd: float, credits_per_1k_tokens: float, signup_bonus: int, max_adjust: int, max_codes_per_batch: int)`
  - `CreditPolicy.from_settings(settings=None) -> CreditPolicy`
  - `get_credit_policy() -> CreditPolicy` / `set_credit_policy(policy: CreditPolicy | None) -> None`
  - `compute_charge(cost_usd: float | None, total_tokens: int | None, policy: CreditPolicy) -> int`
  - `generate_code() -> str`（返回 `XXXX-XXXX-XXXX-XXXX` 展示格式）
  - `normalize_code(raw: str) -> str`（32 字符 Crockford，无分隔符）
  - `hash_code(normalized: str) -> str`（64 位 hex）
  - `code_prefix(normalized: str) -> str`（前 6 字符）
  - `CROCKFORD_ALPHABET: str`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_credits.py`：

```python
"""积分核心逻辑测试：扣分公式与兑换码编解码。纯函数，不碰数据库。"""
from __future__ import annotations

import pytest

from app.credits import (
    CROCKFORD_ALPHABET,
    CreditPolicy,
    code_prefix,
    compute_charge,
    generate_code,
    hash_code,
    normalize_code,
)

POLICY = CreditPolicy(
    enforced=False,
    credits_per_usd=1000.0,
    credits_per_1k_tokens=1.0,
    signup_bonus=0,
    max_adjust=10_000_000,
    max_codes_per_batch=5000,
)


# ---- 扣分公式 --------------------------------------------------------- #

def test_charge_uses_cost_when_known():
    # 0.0123 USD * 1000 = 12.3 -> 向上取整 13
    assert compute_charge(0.0123, 5000, POLICY) == 13


def test_charge_rounds_up_so_a_real_turn_never_costs_zero():
    # 0.0001 USD * 1000 = 0.1 -> 向上取整 1，不能是 0
    assert compute_charge(0.0001, 10, POLICY) == 1


def test_charge_falls_back_to_tokens_when_cost_is_none():
    """未配置定价的模型 usage_cost() 返回 None；没有兜底就是免费额度。"""
    # 3500 tokens / 1000 * 1.0 = 3.5 -> 4
    assert compute_charge(None, 3500, POLICY) == 4


def test_charge_falls_back_to_tokens_when_cost_is_zero():
    assert compute_charge(0.0, 2000, POLICY) == 2


def test_charge_is_zero_when_nothing_was_consumed():
    assert compute_charge(None, None, POLICY) == 0
    assert compute_charge(0.0, 0, POLICY) == 0
    assert compute_charge(None, 0, POLICY) == 0


def test_charge_prefers_cost_over_tokens():
    # cost 已知时完全忽略 token 数量
    assert compute_charge(0.002, 10_000_000, POLICY) == 2


# ---- 码的生成与规范化 -------------------------------------------------- #

def test_generated_code_has_four_groups_of_four():
    code = generate_code()
    parts = code.split("-")
    assert len(parts) == 4
    assert all(len(p) == 4 for p in parts)


def test_generated_code_only_uses_crockford_alphabet():
    for _ in range(200):
        normalized = normalize_code(generate_code())
        assert len(normalized) == 16
        assert set(normalized) <= set(CROCKFORD_ALPHABET)


def test_generated_codes_are_unique():
    assert len({generate_code() for _ in range(500)}) == 500


def test_normalize_accepts_lowercase_and_missing_dashes():
    code = generate_code()
    assert normalize_code(code.lower().replace("-", "")) == normalize_code(code)


def test_normalize_maps_ambiguous_characters():
    """手抄错误：O 打成 0、I/L 打成 1，应该仍然能兑换。"""
    assert normalize_code("oooo-1111-llll-O0I1") == "0000111111110011"


def test_normalize_strips_arbitrary_separators():
    assert normalize_code(" ab12 cd34 ef56 gh78 ") == "AB12CD34EF56GH78"
    assert normalize_code("AB12/ CD34_EF56.GH78") == "AB12CD34EF56GH78"


def test_normalize_is_idempotent():
    code = generate_code()
    once = normalize_code(code)
    assert normalize_code(once) == once


def test_hash_is_64_hex_and_depends_on_normalized_form():
    normalized = normalize_code(generate_code())
    digest = hash_code(normalized)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_ignores_input_formatting():
    code = generate_code()
    assert hash_code(normalize_code(code)) == hash_code(normalize_code(code.lower()))


def test_code_prefix_is_six_chars():
    assert code_prefix("AB12CD34EF56GH78") == "AB12CD"


# ---- 策略解析 ---------------------------------------------------------- #

def test_enforcement_is_forced_off_in_test_env():
    """与 quotas.py / rate_limit.py 的既有约定一致：测试套件默认不被拦截。"""
    from app.core.config import get_settings

    policy = CreditPolicy.from_settings(get_settings())
    assert policy.enforced is False


def test_policy_overrides_are_injectable():
    from dataclasses import replace

    from app.credits import get_credit_policy, set_credit_policy

    set_credit_policy(replace(POLICY, enforced=True))
    try:
        assert get_credit_policy().enforced is True
    finally:
        set_credit_policy(None)
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && python -m pytest tests/test_credits.py -v
```

Expected: FAIL —— `ModuleNotFoundError: No module named 'app.credits'`

- [ ] **Step 3: 加配置项**

在 `backend/app/core/config.py` 的 `QUOTA_RUN_TTL_SECONDS: int = 3600` 之后插入：

```python
    # ---- Credits / redeem codes ----
    # 预付费积分。CREDITS_ENFORCED 默认关 = 观察模式：扣分照常记账、余额照常
    # 显示，但余额不足不拦截。上线时先发码核对扣分数字，再打开拦截。
    CREDITS_ENFORCED: bool = False
    # 1 美元服务端实测成本 = 多少积分。
    CREDITS_PER_USD: float = 1000.0
    # 未配置定价的模型（usage_cost() 返回 None）按 token 兜底扣分，
    # 每 1000 token 记多少积分。没有这条兜底，这些模型就是免费额度。
    CREDITS_PER_1K_TOKENS_FALLBACK: float = 1.0
    # 注册赠送积分；0 = 不送。
    CREDITS_SIGNUP_BONUS: int = 0
    # 单次管理员调分的绝对值上限（防误操作把余额打成天文数字）。
    CREDITS_MAX_ADJUST: int = 10_000_000
    # 单批兑换码生成上限。
    REDEEM_MAX_CODES_PER_BATCH: int = 5000
```

在 `backend/.env.example` 末尾追加：

```bash
# ---- Credits / redeem codes (预付费积分) ----
# 观察模式开关：false = 扣分记账但不拦截（上线初期用），true = 余额不足拒绝请求
CREDITS_ENFORCED=false
# 1 美元服务端实测成本折算多少积分
CREDITS_PER_USD=1000.0
# 未配置定价的模型每 1000 token 兜底扣多少积分
CREDITS_PER_1K_TOKENS_FALLBACK=1.0
# 注册赠送积分（0 = 不送）
CREDITS_SIGNUP_BONUS=0
# 单次管理员调分上限
CREDITS_MAX_ADJUST=10000000
# 单批兑换码生成上限
REDEEM_MAX_CODES_PER_BATCH=5000
```

- [ ] **Step 4: 实现 `app/credits.py`**

```python
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

    规则：去掉所有非字母数字字符、转大写、修正手抄歧义字符（I/L→1、O→0）。
    生成时与哈希前都必须走这条路径，两边一致才能匹配上。
    """
    out: list[str] = []
    for ch in (raw or "").upper():
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
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd backend && python -m pytest tests/test_credits.py -v
```

Expected: PASS —— 全部 17 个用例

- [ ] **Step 6: 提交**

```bash
git add backend/app/credits.py backend/app/core/config.py backend/.env.example backend/tests/test_credits.py
git commit -m "feat(credits): 积分扣分公式与兑换码编解码

扣分优先按服务端实测成本换算，成本未知或为 0 时回落到 token 计价 ——
usage_cost() 对未定价模型返回 None，没有回落就是免费额度。

兑换码用 Crockford Base32（去掉 I/L/O/U），16 字符 80 bit 熵，
规范化时修正手抄歧义字符（I/L→1、O→0），只存 SHA-256 不存明文。"
```

---

### Task 2: 数据模型与迁移

**Files:**
- Create: `backend/app/models/credit_account.py`
- Create: `backend/app/models/credit_ledger.py`
- Create: `backend/app/models/redeem_code_batch.py`
- Create: `backend/app/models/redeem_code.py`
- Create: `backend/migrations/versions/0014_credits_and_redeem_codes.py`
- Modify: `backend/app/models/__init__.py`
- Modify: `backend/docker-compose.prod.yml`（若存在 `0010_artifacts` 过期注释）
- Test: `backend/tests/test_credits_models.py`

**Interfaces:**
- Consumes: `app.db.Base`、`app.models._mixins.TimestampMixin`
- Produces:
  - `CreditAccount(user_id, balance, lifetime_granted, lifetime_consumed)`
  - `CreditLedger(id, user_id, delta, balance_after, reason, ref_type, ref_id, actor_id, note, created_at)`
  - `RedeemCodeBatch(id, name, credits_per_code, expires_at, note, created_by)`
  - `RedeemCode(id, batch_id, code_hash, code_prefix, status, redeemed_by, redeemed_at)`
  - 唯一部分索引名：`uq_credit_ledger_ref`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_credits_models.py`：

```python
"""积分四张表的约束测试。重点验证"不允许出现第二次"的数据库级保证。"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import CreditAccount, CreditLedger, RedeemCode, RedeemCodeBatch


async def test_ledger_ref_triple_is_unique(db_session):
    """同一 (ref_type, ref_id, reason) 只能有一行 —— 这是扣费幂等的根基。"""
    uid = uuid.uuid4()
    for _ in range(2):
        db_session.add(
            CreditLedger(
                user_id=uid,
                delta=-5,
                balance_after=95,
                reason="usage",
                ref_type="message",
                ref_id="msg-1",
            )
        )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_ledger_allows_many_null_ref_rows(db_session):
    """ref_type 为 NULL 的行不受唯一约束限制（管理员调分可重复出现）。"""
    uid = uuid.uuid4()
    db_session.add_all(
        [
            CreditLedger(user_id=uid, delta=10, balance_after=10, reason="admin_adjust"),
            CreditLedger(user_id=uid, delta=10, balance_after=20, reason="admin_adjust"),
        ]
    )
    await db_session.flush()  # 不应抛异常


async def test_ledger_different_reason_same_ref_is_allowed(db_session):
    uid = uuid.uuid4()
    db_session.add_all(
        [
            CreditLedger(
                user_id=uid, delta=100, balance_after=100,
                reason="redeem", ref_type="redeem_code", ref_id="code-1",
            ),
            CreditLedger(
                user_id=uid, delta=-1, balance_after=99,
                reason="usage", ref_type="redeem_code", ref_id="code-1",
            ),
        ]
    )
    await db_session.flush()  # 不应抛异常


async def test_code_hash_is_unique(db_session):
    batch = RedeemCodeBatch(name="b", credits_per_code=100)
    db_session.add(batch)
    await db_session.flush()
    for _ in range(2):
        db_session.add(
            RedeemCode(
                batch_id=batch.id, code_hash="a" * 64, code_prefix="AAAAAA", status="active"
            )
        )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_account_balance_may_go_negative(db_session):
    """准入在轮前、扣费在轮后，最后一轮必然透支。不能加 CHECK 约束。"""
    uid = uuid.uuid4()
    db_session.add(CreditAccount(user_id=uid, balance=-42))
    await db_session.flush()


async def test_batch_rejects_non_positive_credits(db_session):
    db_session.add(RedeemCodeBatch(name="bad", credits_per_code=0))
    with pytest.raises(IntegrityError):
        await db_session.flush()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && python -m pytest tests/test_credits_models.py -v
```

Expected: FAIL —— `ImportError: cannot import name 'CreditAccount' from 'app.models'`

- [ ] **Step 3: 创建四个模型**

`backend/app/models/credit_account.py`：

```python
"""积分账户：每个用户一行，权威余额。

余额是**派生值** —— 真正的真相来源是 :class:`CreditLedger` 的流水求和。
这里单独存一行是为了让准入检查（每轮对话都要做）是 O(1) 单行读，而不是
每次聚合整张账本。两者是否一致由运维对账 SQL 检查（见 docs/credits-operations.md）。

``balance`` 刻意不加 ``CHECK (balance >= 0)``：准入在轮前、扣费在轮后，
最后一轮必然把余额扣成负数（平台确实花了这笔钱）。加了约束只会让合法的
最后一轮被数据库拒绝，并把真实负债藏起来。
"""
from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class CreditAccount(Base, TimestampMixin):
    __tablename__ = "credit_accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # 权威余额，可为负（见模块 docstring）。
    balance: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lifetime_granted: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lifetime_consumed: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
```

`backend/app/models/credit_ledger.py`：

```python
"""积分流水：只追加，永不修改。

账本既是审计轨迹（谁、何时、因为什么、变动多少、变动后余额），也是幂等的
执行机制 —— ``uq_credit_ledger_ref`` 这个唯一部分索引让"同一轮对话扣两次"和
"同一个码兑两次"在数据库层面不可能发生，重试 / 双发 / worker 重复消费都安全。

``ref_type IS NULL`` 的行不受约束（管理员多次调分是合法的重复），所以索引用
的是**部分**唯一索引而不是普通唯一索引。
"""
from __future__ import annotations

import uuid
from datetime import datetime, UTC

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CreditLedger(Base):
    __tablename__ = "credit_ledger"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 正 = 发放，负 = 消耗。
    delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 写这一行之后的余额快照。用于展示与对账，避免为了显示"当时余额"而
    # 反推整张账本。
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # redeem | admin_adjust | usage | signup_bonus
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    # redeem_code | message | admin | NULL
    ref_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 操作管理员；系统扣费为 NULL。
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Python 侧默认（微秒精度），理由同 Message.created_at：server_default
    # 在 Postgres 上取的是事务开始时间，同一事务内多行会同时间戳，分页与
    # 排序都不稳定。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (
        Index("ix_credit_ledger_user_created", "user_id", "created_at"),
        Index(
            "uq_credit_ledger_ref",
            "ref_type",
            "ref_id",
            "reason",
            unique=True,
            # 两个方言都要给 where，否则测试库（SQLite）不会真正强制约束，
            # 幂等测试就会变成假绿。
            postgresql_where=text("ref_type IS NOT NULL"),
            sqlite_where=text("ref_type IS NOT NULL"),
        ),
    )
```

`backend/app/models/redeem_code_batch.py`：

```python
"""兑换码批次：一次生成的一批码共享面额与有效期。

批次是运营单位（"2026 中秋活动，500 张 10000 分的卡"），也是作废与核销进度
统计的单位。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class RedeemCodeBatch(Base, TimestampMixin):
    __tablename__ = "redeem_code_batches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    credits_per_code: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # NULL = 永久有效。
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint("credits_per_code > 0", name="ck_redeem_batch_credits_positive"),
    )
```

`backend/app/models/redeem_code.py`：

```python
"""单张兑换码。

**只存 SHA-256 哈希，不存明文。** 兑换码是不记名凭证，等价于现金：明文入库
意味着任何一次数据库泄露、备份外泄或日志误打都直接变成白送积分。明文只在
生成那一次响应里返回供管理员导出，此后系统内不再有明文。

代价是有意接受的：CSV 丢失后这批码不可恢复，只能整批作废重发。

``code_prefix`` 存前 6 位明文，仅供管理员在列表中辨认某张卡（"用户说他手上
是 AB12CD 开头的那张"），不足以从哈希反推。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RedeemCode(Base):
    __tablename__ = "redeem_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("redeem_code_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    code_prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    # active | redeemed | void
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    redeemed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    redeemed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_redeem_codes_batch_status", "batch_id", "status"),)
```

- [ ] **Step 4: 注册模型**

在 `backend/app/models/__init__.py` 中，把 import 按字母序插入（`Connector` 之后、`Document` 之前）：

```python
from app.models.credit_account import CreditAccount
from app.models.credit_ledger import CreditLedger
from app.models.redeem_code import RedeemCode
from app.models.redeem_code_batch import RedeemCodeBatch
```

并在 `__all__` 列表中追加：

```python
    # ---- Credits / redeem codes (预付费积分) ----
    "CreditAccount",
    "CreditLedger",
    "RedeemCodeBatch",
    "RedeemCode",
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd backend && python -m pytest tests/test_credits_models.py tests/test_credits.py -v
```

Expected: PASS —— 全部用例

- [ ] **Step 6: 写迁移**

创建 `backend/migrations/versions/0014_credits_and_redeem_codes.py`：

```python
"""Credits + redeem codes (预付费积分与兑换码).

Revision ID: 0014_credits_redeem
Revises: 0013_wechat_identities
Create Date: 2026-09-17

四张表：credit_accounts（权威余额）、credit_ledger（只追加账本）、
redeem_code_batches、redeem_codes。

两处关键点：

1. ``uq_credit_ledger_ref`` 是**唯一部分索引**（``WHERE ref_type IS NOT NULL``），
   让"同一轮对话扣两次"和"同一个码兑两次"在数据库层面不可能发生。
2. upgrade 会为所有存量用户**回填** credit_accounts 行。不可省：没有回填，
   老用户就不存在账户行，"余额为 0"和"账户不存在"在排查时会变成两件事。

守卫式写法（与 0012 / 0013 一致）：对由 ``create_all`` 而非迁移建库的库也安全。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_credits_redeem"
down_revision: Union[str, None] = "0013_wechat_identities"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = (
    "credit_accounts",
    "credit_ledger",
    "redeem_code_batches",
    "redeem_codes",
)


def _has_table(table: str) -> bool:
    bind = op.get_bind()
    return table in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    if not _has_table("credit_accounts"):
        op.create_table(
            "credit_accounts",
            sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("balance", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column(
                "lifetime_granted", sa.BigInteger(), nullable=False, server_default="0"
            ),
            sa.Column(
                "lifetime_consumed", sa.BigInteger(), nullable=False, server_default="0"
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["user_id"], ["users.id"], name="fk_credit_accounts_user", ondelete="CASCADE"
            ),
        )

    if not _has_table("credit_ledger"):
        op.create_table(
            "credit_ledger",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("delta", sa.BigInteger(), nullable=False),
            sa.Column("balance_after", sa.BigInteger(), nullable=False),
            sa.Column("reason", sa.String(length=32), nullable=False),
            sa.Column("ref_type", sa.String(length=32), nullable=True),
            sa.Column("ref_id", sa.String(length=64), nullable=True),
            sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["user_id"], ["users.id"], name="fk_credit_ledger_user", ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["actor_id"], ["users.id"], name="fk_credit_ledger_actor", ondelete="SET NULL"
            ),
        )
        op.create_index("ix_credit_ledger_user_id", "credit_ledger", ["user_id"])
        op.create_index(
            "ix_credit_ledger_user_created", "credit_ledger", ["user_id", "created_at"]
        )
        # 幂等根基：同一 (ref_type, ref_id, reason) 只能一行。
        op.create_index(
            "uq_credit_ledger_ref",
            "credit_ledger",
            ["ref_type", "ref_id", "reason"],
            unique=True,
            postgresql_where=sa.text("ref_type IS NOT NULL"),
        )

    if not _has_table("redeem_code_batches"):
        op.create_table(
            "redeem_code_batches",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("credits_per_code", sa.BigInteger(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["created_by"],
                ["users.id"],
                name="fk_redeem_batch_creator",
                ondelete="SET NULL",
            ),
            sa.CheckConstraint(
                "credits_per_code > 0", name="ck_redeem_batch_credits_positive"
            ),
        )

    if not _has_table("redeem_codes"):
        op.create_table(
            "redeem_codes",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("code_hash", sa.String(length=64), nullable=False),
            sa.Column("code_prefix", sa.String(length=8), nullable=False),
            sa.Column(
                "status", sa.String(length=16), nullable=False, server_default="active"
            ),
            sa.Column("redeemed_by", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["batch_id"],
                ["redeem_code_batches.id"],
                name="fk_redeem_codes_batch",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["redeemed_by"], ["users.id"], name="fk_redeem_codes_redeemer", ondelete="SET NULL"
            ),
        )
        op.create_index("ix_redeem_codes_batch_id", "redeem_codes", ["batch_id"])
        op.create_index("ix_redeem_codes_code_hash", "redeem_codes", ["code_hash"], unique=True)
        op.create_index(
            "ix_redeem_codes_batch_status", "redeem_codes", ["batch_id", "status"]
        )

    # 回填：每个存量用户一行账户（余额 0）。幂等，重复执行安全。
    if not _has_table("__never__"):
        op.execute(
            """
            INSERT INTO credit_accounts
                (user_id, balance, lifetime_granted, lifetime_consumed)
            SELECT id, 0, 0, 0 FROM users
            ON CONFLICT (user_id) DO NOTHING
            """
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        if _has_table(table):
            op.drop_table(table)
```

- [ ] **Step 7: 验证迁移可用**

```bash
cd backend && python -m pytest tests/ -k "migration" -v
```

Expected: PASS（既有的迁移一致性测试仍通过；新迁移不引入第二个 head）

- [ ] **Step 8: 修掉过期注释**

`backend/docker-compose.prod.yml:101` 的注释写着迁移 head 是 `0010_artifacts`，实际已是 `0013`（本次之后是 `0014`）。把该行改为指向 `0014_credits_redeem`，避免下次部署时误判。

- [ ] **Step 9: 提交**

```bash
git add backend/app/models/ backend/migrations/versions/0014_credits_and_redeem_codes.py backend/tests/test_credits_models.py backend/docker-compose.prod.yml
git commit -m "feat(credits): 积分账户与兑换码四张表 + 迁移 0014

credit_accounts + credit_ledger + redeem_code_batches + redeem_codes。

uq_credit_ledger_ref 是唯一部分索引（WHERE ref_type IS NOT NULL），
扣费与兑换的幂等由它兜底，不靠应用层自觉。

迁移为所有存量用户回填账户行 —— 否则老用户不存在账户行，
'余额为 0' 和 '账户不存在' 在排查时会变成两件事。"
```

---

### Task 3: credit_service —— 账户、账本、扣分、调分

**Files:**
- Create: `backend/app/services/credit_service.py`
- Test: `backend/tests/test_credit_service.py`

**Interfaces:**
- Consumes: `app.credits`（Task 1）、`app.models`（Task 2）
- Produces:
  - `async def get_or_create_account(db, user_id: uuid.UUID) -> CreditAccount`
  - `async def read_account(db, user_id) -> CreditAccount | None`
  - `async def grant(db, user_id, *, amount: int, reason: str, ref_type=None, ref_id=None, actor_id=None, note=None) -> CreditLedger | None`
  - `async def charge_usage(db, user_id, *, amount: int, ref_type: str, ref_id: str, note=None) -> CreditLedger | None`
  - `async def adjust(db, user_id, *, delta: int, actor_id: uuid.UUID, note: str | None) -> CreditLedger`
  - `async def charge_message_credits(db, user_id, message) -> int`
  - `async def ledger_page(db, user_id, *, limit: int, cursor: str | None = None) -> tuple[list[CreditLedger], str | None]`
  - `async def list_accounts(db, *, search: str | None, limit: int, offset: int) -> list[tuple[User, CreditAccount]]`
  - `def encode_cursor(entry: CreditLedger) -> str` / `def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID] | None`
  - `class CreditError(Exception)`（携带 `code` 与 `message`，由 API 层转成 `AppException`）

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_credit_service.py`：

```python
"""积分服务测试：记账、扣分、幂等、透支、调分、分页。"""
from __future__ import annotations

import uuid

import pytest

from app.models import CreditLedger, Message
from app.services import credit_service

SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def test_account_is_created_lazily_with_zero_balance(db_session):
    uid = uuid.uuid4()
    account = await credit_service.get_or_create_account(db_session, uid)
    assert account.balance == 0
    # 再取一次应该是同一行，不重复创建
    again = await credit_service.get_or_create_account(db_session, uid)
    assert again.user_id == uid


async def test_grant_increases_balance_and_writes_ledger(db_session):
    uid = uuid.uuid4()
    await credit_service.grant(
        db_session, uid, amount=1000, reason="redeem",
        ref_type="redeem_code", ref_id="code-1",
    )
    account = await credit_service.read_account(db_session, uid)
    assert account.balance == 1000
    assert account.lifetime_granted == 1000

    rows, _ = await credit_service.ledger_page(db_session, uid, limit=10)
    assert rows[0].delta == 1000
    assert rows[0].balance_after == 1000


async def test_grant_is_idempotent_on_same_ref(db_session):
    """同一个兑换码只能加一次分 —— 唯一索引兜底，重复调用返回 None。"""
    uid = uuid.uuid4()
    first = await credit_service.grant(
        db_session, uid, amount=500, reason="redeem",
        ref_type="redeem_code", ref_id="code-1",
    )
    second = await credit_service.grant(
        db_session, uid, amount=500, reason="redeem",
        ref_type="redeem_code", ref_id="code-1",
    )
    assert first is not None
    assert second is None
    account = await credit_service.read_account(db_session, uid)
    assert account.balance == 500  # 只加了一次


async def test_charge_usage_deducts_and_records_balance_after(db_session):
    uid = uuid.uuid4()
    await credit_service.grant(db_session, uid, amount=100, reason="admin_adjust")
    await credit_service.charge_usage(
        db_session, uid, amount=30, ref_type="message", ref_id="msg-1"
    )
    account = await credit_service.read_account(db_session, uid)
    assert account.balance == 70
    assert account.lifetime_consumed == 30

    rows, _ = await credit_service.ledger_page(db_session, uid, limit=10)
    # 倒序：最新的在前
    assert rows[0].delta == -30
    assert rows[0].balance_after == 70
    assert rows[1].delta == 100


async def test_charge_usage_is_idempotent_on_same_message(db_session):
    """worker 重复消费 / 重试不能重复扣费。"""
    uid = uuid.uuid4()
    await credit_service.grant(db_session, uid, amount=100, reason="admin_adjust")
    first = await credit_service.charge_usage(
        db_session, uid, amount=30, ref_type="message", ref_id="msg-1"
    )
    second = await credit_service.charge_usage(
        db_session, uid, amount=30, ref_type="message", ref_id="msg-1"
    )
    assert first is not None
    assert second is None
    account = await credit_service.read_account(db_session, uid)
    assert account.balance == 70  # 只扣了一次


async def test_balance_may_go_negative(db_session):
    """最后一轮透支：平台确实花了这笔钱，允许扣成负数。"""
    uid = uuid.uuid4()
    await credit_service.grant(db_session, uid, amount=10, reason="admin_adjust")
    await credit_service.charge_usage(
        db_session, uid, amount=25, ref_type="message", ref_id="msg-1"
    )
    account = await credit_service.read_account(db_session, uid)
    assert account.balance == -15


async def test_adjust_by_admin_records_actor(db_session):
    uid = uuid.uuid4()
    admin = uuid.uuid4()
    await credit_service.adjust(db_session, uid, delta=250, actor_id=admin, note="客服补偿")
    account = await credit_service.read_account(db_session, uid)
    assert account.balance == 250

    rows, _ = await credit_service.ledger_page(db_session, uid, limit=5)
    assert rows[0].reason == "admin_adjust"
    assert rows[0].actor_id == admin
    assert rows[0].note == "客服补偿"


async def test_adjust_rejects_zero_delta(db_session):
    with pytest.raises(credit_service.CreditError) as exc:
        await credit_service.adjust(
            db_session, uuid.uuid4(), delta=0, actor_id=uuid.uuid4(), note=None
        )
    assert exc.value.code == "credit_adjust_zero"


async def test_adjust_rejects_amount_over_policy_cap(db_session):
    from app.credits import CreditPolicy, set_credit_policy

    set_credit_policy(CreditPolicy(max_adjust=100))
    try:
        with pytest.raises(credit_service.CreditError) as exc:
            await credit_service.adjust(
                db_session, uuid.uuid4(), delta=101, actor_id=uuid.uuid4(), note=None
            )
        assert exc.value.code == "credit_adjust_too_large"
    finally:
        set_credit_policy(None)


async def test_ledger_page_paginates_without_gaps_or_repeats(db_session):
    uid = uuid.uuid4()
    for i in range(25):
        await credit_service.grant(
            db_session, uid, amount=1, reason="admin_adjust", note=f"n{i}"
        )
    first, cursor = await credit_service.ledger_page(db_session, uid, limit=10)
    assert len(first) == 10
    assert cursor is not None
    second, cursor2 = await credit_service.ledger_page(
        db_session, uid, limit=10, cursor=cursor
    )
    assert len(second) == 10
    third, cursor3 = await credit_service.ledger_page(
        db_session, uid, limit=10, cursor=cursor2
    )
    assert len(third) == 5
    assert cursor3 is None

    ids = [row.id for row in (*first, *second, *third)]
    assert len(set(ids)) == 25  # 无重复无遗漏


async def test_charge_message_credits_uses_priced_cost(db_session, monkeypatch):
    from app.credits import CreditPolicy, set_credit_policy

    set_credit_policy(CreditPolicy(credits_per_usd=1000.0))
    try:
        uid = uuid.uuid4()
        await credit_service.grant(db_session, uid, amount=10_000, reason="admin_adjust")
        msg = Message(
            id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            role="assistant",
            content="hi",
            metadata_={},
            model_name="gpt-4o",
            prompt_tokens=100,
            completion_tokens=100,
            total_tokens=200,
            cost_usd=0.05,
        )
        charged = await credit_service.charge_message_credits(db_session, uid, msg)
        assert charged == 50  # 0.05 * 1000
        account = await credit_service.read_account(db_session, uid)
        assert account.balance == 9950
    finally:
        set_credit_policy(None)


async def test_charge_message_credits_falls_back_to_tokens_when_unpriced(db_session):
    """未定价模型（cost_usd 为 None）必须按 token 扣，否则是免费额度。"""
    from app.credits import CreditPolicy, set_credit_policy

    set_credit_policy(CreditPolicy(credits_per_usd=1000.0, credits_per_1k_tokens=2.0))
    try:
        uid = uuid.uuid4()
        await credit_service.grant(db_session, uid, amount=10_000, reason="admin_adjust")
        msg = Message(
            id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            role="assistant",
            content="hi",
            metadata_={},
            model_name="some-unpriced-model",
            prompt_tokens=1500,
            completion_tokens=500,
            total_tokens=2000,
            cost_usd=None,
        )
        charged = await credit_service.charge_message_credits(db_session, uid, msg)
        assert charged == 4  # 2000 / 1000 * 2.0
    finally:
        set_credit_policy(None)


async def test_charge_message_credits_is_zero_for_no_usage(db_session):
    uid = uuid.uuid4()
    msg = Message(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        role="assistant",
        content="",
        metadata_={},
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        cost_usd=None,
    )
    assert await credit_service.charge_message_credits(db_session, uid, msg) == 0
    assert await credit_service.read_account(db_session, uid) is None  # 未创建账户行
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && python -m pytest tests/test_credit_service.py -v
```

Expected: FAIL —— `ModuleNotFoundError: No module named 'app.services.credit_service'`

- [ ] **Step 3: 实现 `app/services/credit_service.py`**

```python
"""积分账户与账本的读写。

三条并发纪律，全部落在数据库机制上而非应用层自觉：

1. **余额变动先锁账户行** —— :func:`_locked_account` 用 ``SELECT ... FOR UPDATE``。
   Postgres 上渲染成真行锁；SQLite 方言直接忽略该子句，而 SQLite 本身是单写者
   模型，所以测试库语义依然正确，一套代码不用分叉。
2. **发放 / 扣费幂等** —— 靠 ``uq_credit_ledger_ref`` 唯一部分索引。重复调用会
   撞约束，捕获后按幂等命中处理（返回 ``None``）。捕获时必须用
   ``begin_nested()`` 开 SAVEPOINT，否则 IntegrityError 会毒化整个外层事务
   （Postgres 上后续任何语句都会失败）。
3. **余额只通过本模块变动** —— 别处直接改 ``CreditAccount.balance`` 会绕过账本，
   让对账 SQL 报警。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, UTC

from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.credits import compute_charge, get_credit_policy
from app.models import CreditAccount, CreditLedger, Message, User

logger = logging.getLogger(__name__)


class CreditError(Exception):
    """积分业务错误。API 层捕获后转成 :class:`AppException`。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _is_unique_violation(exc: IntegrityError) -> bool:
    """跨方言判断是否唯一约束冲突。

    psycopg/asyncpg 用 SQLSTATE 23505，SQLite 用 "UNIQUE constraint failed"
    文案。两者都判断，避免为了可移植性引入方言分支。
    """
    orig = getattr(exc, "orig", None)
    if getattr(orig, "pgcode", None) == "23505":
        return True
    return "unique" in str(orig).lower()


# --------------------------------------------------------------------------- #
# 账户
# --------------------------------------------------------------------------- #
async def read_account(db: AsyncSession, user_id: uuid.UUID) -> CreditAccount | None:
    """只读账户，不存在返回 None（不创建）。"""
    return await db.get(CreditAccount, user_id)


async def get_or_create_account(
    db: AsyncSession, user_id: uuid.UUID
) -> CreditAccount:
    """取得账户行，不存在则创建。

    正常路径下账户行永远存在（迁移回填 + 注册时创建），这里是兜底。
    并发创建时输的一方撞唯一约束，回滚到 SAVEPOINT 后重查即可。
    """
    account = await db.get(CreditAccount, user_id)
    if account is not None:
        return account
    try:
        async with db.begin_nested():
            account = CreditAccount(user_id=user_id, balance=0)
            db.add(account)
            await db.flush()
        return account
    except IntegrityError:
        # 并发下别人先建好了 —— 重新取一次。
        account = await db.get(CreditAccount, user_id)
        if account is None:  # pragma: no cover - 只可能在异常被误判时发生
            raise
        return account


async def _locked_account(db: AsyncSession, user_id: uuid.UUID) -> CreditAccount:
    """取账户行并加行锁。所有余额变动的唯一入口。

    ``populate_existing=True`` 不是可选项：会话的 identity map 里可能已经有这个
    account 对象，而 SQLAlchemy 默认**不会**用查询结果覆盖已加载的属性。那样
    即使 ``FOR UPDATE`` 拿到了新行，``account.balance`` 仍是旧值，
    :func:`_write_entry` 会基于陈旧的余额算出错误的 ``balance_after`` 和最终
    余额 —— 在并发充值/扣费下就是直接算错钱。加上它强制用锁定读到的新值覆盖。
    """
    await get_or_create_account(db, user_id)
    result = await db.execute(
        select(CreditAccount)
        .where(CreditAccount.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def _write_entry(
    db: AsyncSession,
    account: CreditAccount,
    *,
    delta: int,
    reason: str,
    ref_type: str | None,
    ref_id: str | None,
    actor_id: uuid.UUID | None,
    note: str | None,
) -> CreditLedger | None:
    """写流水 + 更新余额。唯一约束冲突时返回 None（幂等命中）。

    调用方必须已经持有账户行锁（见 :func:`_locked_account`），否则并发下
    ``balance_after`` 会算错。
    """
    new_balance = int(account.balance) + int(delta)
    entry = CreditLedger(
        user_id=account.user_id,
        delta=int(delta),
        balance_after=new_balance,
        reason=reason,
        ref_type=ref_type,
        ref_id=ref_id,
        actor_id=actor_id,
        note=note,
    )
    try:
        async with db.begin_nested():
            db.add(entry)
            await db.flush()
    except IntegrityError as exc:
        if not _is_unique_violation(exc):
            raise
        return None

    account.balance = new_balance
    if delta > 0:
        account.lifetime_granted = int(account.lifetime_granted) + int(delta)
    elif delta < 0:
        account.lifetime_consumed = int(account.lifetime_consumed) + (-int(delta))
    await db.flush()
    return entry


# --------------------------------------------------------------------------- #
# 发放 / 扣费 / 调分
# --------------------------------------------------------------------------- #
async def grant(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    amount: int,
    reason: str,
    ref_type: str | None = None,
    ref_id: str | None = None,
    actor_id: uuid.UUID | None = None,
    note: str | None = None,
) -> CreditLedger | None:
    """发放积分。``amount`` 必须为正。相同 ref 重复调用返回 None。"""
    if amount <= 0:
        raise CreditError("credit_grant_invalid", "发放积分必须为正数")
    account = await _locked_account(db, user_id)
    return await _write_entry(
        db,
        account,
        delta=int(amount),
        reason=reason,
        ref_type=ref_type,
        ref_id=ref_id,
        actor_id=actor_id,
        note=note,
    )


async def charge_usage(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    amount: int,
    ref_type: str,
    ref_id: str,
    note: str | None = None,
) -> CreditLedger | None:
    """扣减积分。相同 ``(ref_type, ref_id, 'usage')`` 重复调用返回 None。"""
    if amount <= 0:
        return None
    account = await _locked_account(db, user_id)
    return await _write_entry(
        db,
        account,
        delta=-int(amount),
        reason="usage",
        ref_type=ref_type,
        ref_id=ref_id,
        actor_id=None,
        note=note,
    )


async def adjust(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    delta: int,
    actor_id: uuid.UUID,
    note: str | None,
) -> CreditLedger:
    """管理员手动调分。``delta`` 非零且绝对值不超过策略上限。

    调分不设唯一约束（``ref_type=None``），管理员可以多次调整同一用户 —— 这
    正是唯一索引必须是**部分**索引的原因。
    """
    if int(delta) == 0:
        raise CreditError("credit_adjust_zero", "调分数量不能为 0")
    cap = get_credit_policy().max_adjust
    if abs(int(delta)) > cap:
        raise CreditError(
            "credit_adjust_too_large",
            f"单次调分绝对值不能超过 {cap}",
        )
    account = await _locked_account(db, user_id)
    entry = await _write_entry(
        db,
        account,
        delta=int(delta),
        reason="admin_adjust",
        ref_type=None,
        ref_id=None,
        actor_id=actor_id,
        note=note,
    )
    if entry is None:  # pragma: no cover - ref 为 NULL 不会撞唯一约束
        raise CreditError("credit_adjust_failed", "调分失败")
    return entry


# --------------------------------------------------------------------------- #
# 挂到聊天轮次上的扣费
# --------------------------------------------------------------------------- #
async def charge_message_credits(
    db: AsyncSession, user_id: uuid.UUID, message: Message
) -> int:
    """按 ``message`` 上已落地的服务端用量扣积分，返回实际扣减数（0 = 未扣）。

    读的是 :func:`app.services.chat_service._apply_usage_accounting` 写入的
    权威字段（服务端实测，永不信客户端）。幂等由 ``ref_type='message'`` 的
    唯一索引保证。

    零消耗（无 usage 的失败轮、mock 响应）不扣，也不创建账户行 —— 避免给
    从未产生消耗的用户凭空建行。
    """
    policy = get_credit_policy()
    amount = compute_charge(
        message.cost_usd,
        message.total_tokens or (
            (message.prompt_tokens or 0) + (message.completion_tokens or 0)
        ),
        policy,
    )
    if amount <= 0:
        return 0
    if getattr(message, "id", None) is None:
        # 防御：正常路径下 assistant 占位行在流式开始前就已提交，id 一定在。
        await db.flush()
    entry = await charge_usage(
        db,
        user_id,
        amount=amount,
        ref_type="message",
        ref_id=str(message.id),
    )
    return amount if entry is not None else 0


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def encode_cursor(entry: CreditLedger) -> str:
    """游标 = ``{created_at.isoformat()}_{id}``。

    次级排序键用 id，避免同一时间戳的流水在分页时漏读或重读。
    """
    return f"{entry.created_at.isoformat()}_{entry.id}"


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID] | None:
    """解析游标。格式非法返回 None（按第一页处理，不报错）。"""
    try:
        ts, _, raw_id = cursor.rpartition("_")
        return datetime.fromisoformat(ts), uuid.UUID(raw_id)
    except (ValueError, AttributeError):
        return None


async def ledger_page(
    db: AsyncSession, user_id: uuid.UUID, *, limit: int, cursor: str | None = None
) -> tuple[list[CreditLedger], str | None]:
    """倒序流水页。返回 ``(rows, next_cursor)``，``next_cursor`` 为 None 表示到底。"""
    size = max(1, min(int(limit), 200))
    stmt = select(CreditLedger).where(CreditLedger.user_id == user_id)
    if cursor:
        decoded = decode_cursor(cursor)
        if decoded is not None:
            ts, entry_id = decoded
            stmt = stmt.where(
                tuple_(CreditLedger.created_at, CreditLedger.id) < tuple_(ts, entry_id)
            )
    # 多取一行用于判断是否还有下一页。
    stmt = stmt.order_by(
        CreditLedger.created_at.desc(), CreditLedger.id.desc()
    ).limit(size + 1)
    rows = list((await db.execute(stmt)).scalars().all())
    has_more = len(rows) > size
    rows = rows[:size]
    return rows, (encode_cursor(rows[-1]) if has_more and rows else None)


async def list_accounts(
    db: AsyncSession, *, search: str | None, limit: int, offset: int
) -> list[tuple[User, CreditAccount]]:
    """用户余额列表（后台用）。搜索匹配邮箱或用户名。"""
    stmt = (
        select(User, CreditAccount)
        .outerjoin(CreditAccount, CreditAccount.user_id == User.id)
        .order_by(User.created_at.desc())
        .limit(max(1, min(int(limit), 500)))
        .offset(max(0, int(offset)))
    )
    if search:
        needle = f"%{search.strip()}%"
        stmt = stmt.where(User.email.ilike(needle) | User.username.ilike(needle))
    return [(row[0], row[1]) for row in (await db.execute(stmt)).all()]
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && python -m pytest tests/test_credit_service.py -v
```

Expected: PASS —— 全部用例

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/credit_service.py backend/tests/test_credit_service.py
git commit -m "feat(credits): credit_service 账户、账本、扣分与调分

余额变动先 SELECT FOR UPDATE 锁账户行（SQLite 方言忽略该子句，
而它本身是单写者，测试库语义仍正确，一套代码不分叉）。

幂等靠唯一部分索引，捕获 IntegrityError 时必须用 begin_nested()
开 SAVEPOINT，否则 Postgres 上 IntegrityError 会毒化整个外层事务。

扣分读 message 上已落地的服务端实测字段；未定价模型走 token 兜底。"
```

---

### Task 4: redeem_service —— 批次生成与兑换

**Files:**
- Create: `backend/app/services/redeem_service.py`
- Test: `backend/tests/test_redeem_service.py`

**Interfaces:**
- Consumes: `app.credits`（Task 1）、`app.models`（Task 2）、`app.services.credit_service`（Task 3）
- Produces:
  - `@dataclass RedeemResult(credits_added: int, balance: int, batch_name: str)`
  - `@dataclass BatchProgress(batch: RedeemCodeBatch, total: int, redeemed: int, void: int, active: int)`
  - `async def create_batch(db, *, admin_id, name, credits_per_code, count, expires_at=None, note=None) -> tuple[RedeemCodeBatch, list[str]]`
  - `async def redeem(db, *, user_id, raw_code) -> RedeemResult`（失败抛 `credit_service.CreditError`）
  - `async def list_batches(db, *, limit=100) -> list[BatchProgress]`
  - `async def list_codes(db, *, batch_id, limit=200, offset=0) -> list[RedeemCode]`
  - `async def void_batch(db, *, batch_id, actor_id) -> int`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_redeem_service.py`：

```python
"""兑换码服务测试：生成、兑换、一次性、过期、作废、明文不落库。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, UTC

import pytest

from app.credits import normalize_code, hash_code
from app.models import RedeemCode, RedeemCodeBatch
from app.services import credit_service, redeem_service

ADMIN = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


async def _make_batch(db, count=3, credits=500, expires_at=None):
    return await redeem_service.create_batch(
        db,
        admin_id=ADMIN,
        name="测试批次",
        credits_per_code=credits,
        count=count,
        expires_at=expires_at,
    )


async def test_create_batch_returns_plaintext_once(db_session):
    batch, codes = await _make_batch(db_session, count=5, credits=500)
    assert len(codes) == 5
    assert len(set(codes)) == 5
    assert batch.id is not None

    # 库里只有哈希，没有明文
    rows = (await db_session.execute(
        RedeemCode.__table__.select().where(RedeemCode.batch_id == batch.id)
    )).all()
    stored = {row.code_hash for row in rows}
    assert stored == {hash_code(normalize_code(c)) for c in codes}
    for row in rows:
        assert row.status == "active"
        assert row.redeemed_by is None
        assert len(row.code_prefix) == 6

async def test_create_batch_rejects_count_over_cap(db_session):
    from app.credits import CreditPolicy, set_credit_policy

    set_credit_policy(CreditPolicy(max_codes_per_batch=10))
    try:
        with pytest.raises(credit_service.CreditError) as exc:
            await _make_batch(db_session, count=11)
        assert exc.value.code == "redeem_batch_too_large"
    finally:
        set_credit_policy(None)


async def test_create_batch_rejects_non_positive_credits(db_session):
    with pytest.raises(credit_service.CreditError) as exc:
        await redeem_service.create_batch(
            db_session, admin_id=ADMIN, name="x", credits_per_code=0, count=1
        )
    assert exc.value.code == "redeem_batch_invalid_credits"


async def test_redeem_adds_credits_and_marks_code(db_session):
    batch, codes = await _make_batch(db_session, count=1, credits=500)
    user = uuid.uuid4()

    result = await redeem_service.redeem(db_session, user_id=user, raw_code=codes[0])
    assert result.credits_added == 500
    assert result.balance == 500
    assert result.batch_name == "测试批次"

    code = (await db_session.execute(
        RedeemCode.__table__.select().where(RedeemCode.batch_id == batch.id)
    )).first()
    assert code.status == "redeemed"
    assert code.redeemed_by == user
    assert code.redeemed_at is not None


async def test_redeem_is_insensitive_to_formatting(db_session):
    _, codes = await _make_batch(db_session, count=1, credits=100)
    messy = codes[0].lower().replace("-", " ")
    result = await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=messy)
    assert result.credits_added == 100


async def test_redeem_twice_fails_and_does_not_double_credit(db_session):
    _, codes = await _make_batch(db_session, count=1, credits=500)
    user = uuid.uuid4()
    await redeem_service.redeem(db_session, user_id=user, raw_code=codes[0])

    with pytest.raises(credit_service.CreditError) as exc:
        await redeem_service.redeem(db_session, user_id=user, raw_code=codes[0])
    assert exc.value.code == "redeem_code_used"
    assert exc.value.status_code == 409

    account = await credit_service.read_account(db_session, user)
    assert account.balance == 500  # 没有被加第二次


async def test_redeem_second_user_cannot_use_same_code(db_session):
    _, codes = await _make_batch(db_session, count=1, credits=500)
    await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[0])
    with pytest.raises(credit_service.CreditError) as exc:
        await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[0])
    assert exc.value.code == "redeem_code_used"


async def test_redeem_unknown_code(db_session):
    with pytest.raises(credit_service.CreditError) as exc:
        await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code="ZZZZ-ZZZZ-ZZZZ-ZZZZ")
    assert exc.value.code == "redeem_code_not_found"
    assert exc.value.status_code == 404


async def test_redeem_empty_code(db_session):
    with pytest.raises(credit_service.CreditError) as exc:
        await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code="   ")
    assert exc.value.code == "redeem_code_not_found"


async def test_redeem_expired_code(db_session):
    past = datetime.now(UTC) - timedelta(days=1)
    _, codes = await _make_batch(db_session, count=1, credits=500, expires_at=past)
    with pytest.raises(credit_service.CreditError) as exc:
        await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[0])
    assert exc.value.code == "redeem_code_expired"
    assert exc.value.status_code == 410


async def test_redeem_code_expiring_in_the_future_works(db_session):
    future = datetime.now(UTC) + timedelta(days=1)
    _, codes = await _make_batch(db_session, count=1, credits=500, expires_at=future)
    result = await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[0])
    assert result.credits_added == 500


async def test_void_batch_kills_only_active_codes(db_session):
    batch, codes = await _make_batch(db_session, count=3, credits=500)
    # 先兑掉一个
    await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[0])

    voided = await redeem_service.void_batch(db_session, batch_id=batch.id)
    assert voided == 2  # 只剩 2 个 active

    with pytest.raises(credit_service.CreditError) as exc:
        await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[1])
    assert exc.value.code == "redeem_code_void"


async def test_void_batch_leaves_already_redeemed_alone(db_session):
    batch, codes = await _make_batch(db_session, count=2, credits=500)
    user = uuid.uuid4()
    await redeem_service.redeem(db_session, user_id=user, raw_code=codes[0])
    await redeem_service.void_batch(db_session, batch_id=batch.id)
    # 已兑换的分数不受作废影响
    account = await credit_service.read_account(db_session, user)
    assert account.balance == 500


async def test_list_batches_reports_progress(db_session):
    batch, codes = await _make_batch(db_session, count=4, credits=100)
    await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[0])
    await redeem_service.void_batch(db_session, batch_id=batch.id)

    rows = await redeem_service.list_batches(db_session)
    target = next(r for r in rows if r.batch.id == batch.id)
    assert target.total == 4
    assert target.redeemed == 1
    assert target.void == 3
    assert target.active == 0


async def test_list_codes_never_returns_plaintext(db_session):
    batch, _ = await _make_batch(db_session, count=3, credits=100)
    rows = await redeem_service.list_codes(db_session, batch_id=batch.id)
    assert len(rows) == 3
    for row in rows:
        # 只暴露前缀，且前缀不含分隔符
        assert len(row.code_prefix) == 6
        assert "-" not in row.code_prefix


# ---- CAS 机制 ------------------------------------------------------------- #

async def test_cas_rejects_a_code_whose_pre_read_was_stale(db_session):
    """CAS 的判定依据是 UPDATE 当时的 ``status``，不是之前的 SELECT。

    这正是"先 SELECT 判断再 UPDATE"会踩的坑：READ COMMITTED 下两个事务可以
    同时把同一张码读成 ``active``，然后**都**通过应用层判断、**都**去加分。
    CAS 把判定压进 UPDATE 的 WHERE，抢输的一方拿到 0 行。

    测试手段：先在一个会话里把码按 active 读出来（模拟第二个事务的陈旧预读），
    然后用另一个会话把它兑掉并提交，最后对那个陈旧对象执行 CAS —— 必须是 0 行。

    SQLite 无法制造真并发（单写者），但可以不依赖并发地把这个时序摆出来，
    验证的是同一段 SQL 语义。真多写者的部署冒烟见
    ``docs/credits-operations.md`` 的并发演练小节。
    """
    from datetime import datetime, UTC

    from sqlalchemy import select, update

    from app.credits import hash_code, normalize_code
    from app.models import RedeemCode

    batch, codes = await _make_batch(db_session, count=1, credits=500)
    await db_session.commit()

    code = (
        await db_session.execute(
            select(RedeemCode).where(
                RedeemCode.code_hash == hash_code(normalize_code(codes[0]))
            )
        )
    ).scalar_one()
    assert code.status == "active"  # 陈旧预读

    # 另一个事务抢先把这张码兑掉。
    await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[0])
    await db_session.commit()

    # 迟到的 CAS：必须 0 行。
    late = await db_session.execute(
        update(RedeemCode)
        .where(RedeemCode.id == code.id, RedeemCode.status == "active")
        .values(status="redeemed", redeemed_by=uuid.uuid4(), redeemed_at=datetime.now(UTC))
    )
    assert late.rowcount == 0


async def test_redeem_reports_used_for_a_code_redeemed_after_pre_read(db_session):
    """同样的时序走服务层：必须先读到 active 才可能走到 CAS，所以直接验结果。"""
    batch, codes = await _make_batch(db_session, count=1, credits=500)
    await db_session.commit()
    await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[0])
    await db_session.commit()

    with pytest.raises(credit_service.CreditError) as exc:
        await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[0])
    assert exc.value.code == "redeem_code_used"
    assert exc.value.status_code == 409


async def test_concurrent_grants_accumulate_rather_than_overwrite(db_session):
    """多次发放必须累加，不能被后一次覆盖。

    行锁（``SELECT ... FOR UPDATE`` + ``populate_existing``）保证读到的余额是最新
    的，所以每次都是"在最新值上加"而不是"在陈旧值上加"。SQLite 上这条退化为
    顺序执行，但同样能抓住"用陈旧值覆盖"这个缺陷 —— 那正是缺了
    ``populate_existing`` 或漏了行锁时的表现。
    """
    user = uuid.uuid4()
    await credit_service.get_or_create_account(db_session, user)
    await db_session.commit()

    for _ in range(5):
        await credit_service.grant(db_session, user, amount=100, reason="admin_adjust")
    await db_session.commit()

    account = await credit_service.read_account(db_session, user)
    assert account.balance == 500
    assert account.lifetime_granted == 500
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && python -m pytest tests/test_redeem_service.py -v
```

Expected: FAIL —— `ModuleNotFoundError: No module named 'app.services.redeem_service'`

- [ ] **Step 3: 实现 `app/services/redeem_service.py`**

```python
"""兑换码批次管理与兑换。

兑换是一个 CAS（compare-and-swap），不用应用层锁：

    UPDATE redeem_codes SET status='redeemed', ... WHERE id=? AND status='active'

``rowcount == 1`` 才算抢到。并发下只有一个事务能赢，输的一方拿到 0 行，走
"已被兑换"分支。这比"先 SELECT 判断再 UPDATE"可靠 —— 后者在 READ COMMITTED
下两个事务可以同时读到 active。

账本侧还有第二道闸：``credit_service.grant`` 用 ``ref_type='redeem_code'`` 的
唯一约束保证同一个码只会加一次分。CAS 与唯一约束各自独立，任一生效都不会
出现重复加分。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, UTC

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.credits import (
    code_prefix,
    generate_code,
    get_credit_policy,
    hash_code,
    normalize_code,
)
from app.models import RedeemCode, RedeemCodeBatch
from app.services import credit_service
from app.services.credit_service import CreditError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RedeemResult:
    credits_added: int
    balance: int
    batch_name: str


@dataclass(frozen=True)
class BatchProgress:
    batch: RedeemCodeBatch
    total: int
    redeemed: int
    void: int
    active: int


async def create_batch(
    db: AsyncSession,
    *,
    admin_id: uuid.UUID | None,
    name: str,
    credits_per_code: int,
    count: int,
    expires_at: datetime | None = None,
    note: str | None = None,
) -> tuple[RedeemCodeBatch, list[str]]:
    """生成一批码，返回 ``(批次, 明文码列表)``。

    **这是明文码唯一一次出现的地方。** 库里只写哈希，调用方必须立刻展示 /
    导出，之后系统内再也拿不到明文。
    """
    policy = get_credit_policy()
    if int(credits_per_code) <= 0:
        raise CreditError("redeem_batch_invalid_credits", "每个兑换码的积分必须为正数")
    if int(count) <= 0:
        raise CreditError("redeem_batch_invalid_count", "生成数量必须为正数")
    if int(count) > policy.max_codes_per_batch:
        raise CreditError(
            "redeem_batch_too_large",
            f"单批最多生成 {policy.max_codes_per_batch} 个兑换码",
        )

    batch = RedeemCodeBatch(
        name=(name or "").strip() or "未命名批次",
        credits_per_code=int(credits_per_code),
        expires_at=expires_at,
        note=note,
        created_by=admin_id,
    )
    db.add(batch)
    await db.flush()

    plaintext: list[str] = []
    seen: set[str] = set()
    for _ in range(int(count)):
        # 极小概率与同批已生成的明文碰撞（也极可能与库里已存在的碰撞）——
        # 重试几次，用哈希去重。
        for _attempt in range(8):
            candidate = generate_code()
            normalized = normalize_code(candidate)
            digest = hash_code(normalized)
            if digest in seen:
                continue
            seen.add(digest)
            plaintext.append(candidate)
            db.add(
                RedeemCode(
                    batch_id=batch.id,
                    code_hash=digest,
                    code_prefix=code_prefix(normalized),
                    status="active",
                )
            )
            break
        else:  # pragma: no cover - 80 bit 空间下不可能连续 8 次碰撞
            raise CreditError("redeem_code_collision", "兑换码生成冲突，请重试")

    await db.flush()
    return batch, plaintext


async def redeem(db: AsyncSession, *, user_id: uuid.UUID, raw_code: str) -> RedeemResult:
    """兑换一个码。失败抛 :class:`CreditError`（带 HTTP 状态码与稳定 code）。"""
    normalized = normalize_code(raw_code or "")
    if not normalized:
        raise CreditError("redeem_code_not_found", "兑换码不存在，请检查是否输入有误", 404)

    row = (
        await db.execute(
            select(RedeemCode, RedeemCodeBatch)
            .join(RedeemCodeBatch, RedeemCode.batch_id == RedeemCodeBatch.id)
            .where(RedeemCode.code_hash == hash_code(normalized))
        )
    ).first()
    if row is None:
        raise CreditError("redeem_code_not_found", "兑换码不存在，请检查是否输入有误", 404)

    code, batch = row
    if code.status == "void":
        raise CreditError("redeem_code_void", "该兑换码已作废", 410)
    if code.status == "redeemed":
        raise CreditError("redeem_code_used", "该兑换码已被使用", 409)
    if batch.expires_at is not None:
        expires_at = batch.expires_at
        if expires_at.tzinfo is None:  # SQLite 取回来可能是 naive
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            raise CreditError("redeem_code_expired", "该兑换码已过期", 410)

    # CAS：并发下只有一个事务能拿到 rowcount == 1。
    claimed = await db.execute(
        update(RedeemCode)
        .where(RedeemCode.id == code.id, RedeemCode.status == "active")
        .values(status="redeemed", redeemed_by=user_id, redeemed_at=datetime.now(UTC))
    )
    if claimed.rowcount != 1:
        # 同一瞬间被别人抢走了。
        raise CreditError("redeem_code_used", "该兑换码已被使用", 409)

    await credit_service.grant(
        db,
        user_id,
        amount=int(batch.credits_per_code),
        reason="redeem",
        ref_type="redeem_code",
        ref_id=str(code.id),
        actor_id=user_id,
        note=batch.name,
    )
    account = await credit_service.read_account(db, user_id)
    await db.flush()
    return RedeemResult(
        credits_added=int(batch.credits_per_code),
        balance=int(account.balance) if account else int(batch.credits_per_code),
        batch_name=batch.name,
    )


async def list_batches(db: AsyncSession, *, limit: int = 100) -> list[BatchProgress]:
    """批次列表含核销进度。一次聚合查询，不是每批一条 count。"""
    progress = (
        select(
            RedeemCode.batch_id.label("batch_id"),
            func.count().label("total"),
            func.sum(_status_flag("redeemed")).label("redeemed"),
            func.sum(_status_flag("void")).label("void"),
            func.sum(_status_flag("active")).label("active"),
        )
        .group_by(RedeemCode.batch_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(RedeemCodeBatch, progress.c.total, progress.c.redeemed, progress.c.void, progress.c.active)
            .outerjoin(progress, progress.c.batch_id == RedeemCodeBatch.id)
            .order_by(RedeemCodeBatch.created_at.desc())
            .limit(max(1, min(int(limit), 500)))
        )
    ).all()
    return [
        BatchProgress(
            batch=row[0],
            total=int(row[1] or 0),
            redeemed=int(row[2] or 0),
            void=int(row[3] or 0),
            active=int(row[4] or 0),
        )
        for row in rows
    ]


def _status_flag(status: str):
    """把 ``status == '<value>'`` 变成一个 0/1 求和项。"""
    from sqlalchemy import case

    return case((RedeemCode.status == status, 1), else_=0)


async def list_codes(
    db: AsyncSession, *, batch_id: uuid.UUID, limit: int = 200, offset: int = 0
) -> list[RedeemCode]:
    rows = (
        await db.execute(
            select(RedeemCode)
            .where(RedeemCode.batch_id == batch_id)
            .order_by(RedeemCode.created_at.asc())
            .limit(max(1, min(int(limit), 1000)))
            .offset(max(0, int(offset)))
        )
    ).scalars().all()
    return list(rows)


async def void_batch(db: AsyncSession, *, batch_id: uuid.UUID) -> int:
    """作废该批所有 ``active`` 码，返回作废数量。已兑换的不受影响。"""
    result = await db.execute(
        update(RedeemCode)
        .where(RedeemCode.batch_id == batch_id, RedeemCode.status == "active")
        .values(status="void")
    )
    await db.flush()
    return int(result.rowcount or 0)
```

`_status_flag` 用 `case` 把 `status == '<value>'` 变成 0/1 求和项 —— 直接写
`func.cast(status == ..., Integer)` 在不同方言下表现不一致，条件求和是稳的。

作废是破坏性操作，审计事件由**路由层**写（`app/api/credits.py`），不在这里 ——
服务层不依赖审计服务，保持可单独测试。

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && python -m pytest tests/test_redeem_service.py -v
```

Expected: PASS —— 全部用例

**事务边界约定（本模块唯一的规则，不要改）**：整个新增的后端服务层
—— `credit_service` 与 `redeem_service` —— **一律只 `flush`，从不 `commit`**。
提交由路由层（`app/api/credits.py`）负责。

原因：聊天扣分必须与消息写入处在同一个事务里，否则会出现"消息存了但钱没扣"。
服务层一旦自己提交，这个原子性就没了。一条规则贯穿全部服务函数，不需要分情况记忆。

Task 4 的测试因此不需要提交就能读回自己写的行 —— 同一个 session 可见。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/redeem_service.py backend/tests/test_redeem_service.py
git commit -m "feat(credits): redeem_service 批次生成与兑换

兑换用单语句 CAS（UPDATE ... WHERE status='active'）而非"先查后改"，
后者在 READ COMMITTED 下两个事务能同时读到 active。

账本侧还有第二道闸：grant 的 ref_type='redeem_code' 唯一约束保证同一个
码只会加一次分。两道闸各自独立，任一生效都不会重复加分。

明文码只在 create_batch 的返回值里出现一次；列表接口只暴露 6 位前缀。"
```

---

### Task 5: Schema 与 API 路由

**Files:**
- Create: `backend/app/schemas/credit.py`
- Create: `backend/app/api/credits.py`
- Modify: `backend/app/schemas/__init__.py`
- Modify: `backend/app/main.py`（在 `wechat.router` 之后挂载）
- Test: `backend/tests/test_credits_api.py`

**Interfaces:**
- Consumes: Task 1–4 全部
- Produces（供前端 Task 8 对齐）：
  - `GET /api/credits/me` → `{balance, lifetime_granted, lifetime_consumed, enforced}`
  - `POST /api/credits/redeem` body `{code}` → `{credits_added, balance, batch_name}`
  - `GET /api/credits/ledger?limit=&cursor=` → `{entries: [...], next_cursor}`
  - `POST /api/admin/redeem-batches` body `{name, credits_per_code, count, expires_at?, note?}` → `{batch: {...}, codes: [str]}`
  - `GET /api/admin/redeem-batches` → `[{batch: {...}, total, redeemed, void, active}]`
  - `GET /api/admin/redeem-batches/{id}/codes?limit=&offset=` → `[{id, code_prefix, status, redeemed_by, redeemed_at, created_at}]`
  - `POST /api/admin/redeem-batches/{id}/void` → `{voided: int}`
  - `GET /api/admin/credits/accounts?search=&limit=&offset=` → `[{user_id, email, username, balance, lifetime_granted, lifetime_consumed}]`
  - `POST /api/admin/credits/adjust` body `{user_id, delta, note?}` → `{balance, ...}`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_credits_api.py`：

```python
"""积分 API 测试：权限、错误码、兑换全链路。

**余额断言一律用全新注册的用户**，不要用种子用户。整个测试套件共用同一个
内存库（session 级的 ``seeded_db``），对种子用户做绝对余额断言会随执行顺序
变化而失败 —— 那是假阳性，会耗掉排查时间。
"""
from __future__ import annotations

import uuid

import pytest

from tests.conftest import auth_headers


async def _fresh_user(client) -> tuple[dict[str, str], dict]:
    """注册一个全新用户，返回 ``(请求头, 用户对象)``。

    用户对象来自注册响应的 ``user`` 字段，含 ``id`` / ``email`` / ``username``。
    """
    suffix = uuid.uuid4().hex[:8]
    res = await client.post(
        "/api/auth/register",
        json={
            "email": f"u{suffix}@example.com",
            "username": f"u{suffix}",
            "password": "FreshPass123",
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    return {"Authorization": f"Bearer {body['access_token']}"}, body["user"]


async def _create_batch(client, admin_token, count=3, credits=500, **over):
    body = {"name": "API 批次", "credits_per_code": credits, "count": count, **over}
    res = await client.post(
        "/api/admin/redeem-batches", json=body, headers=auth_headers(admin_token)
    )
    assert res.status_code == 200, res.text
    return res.json()


async def test_me_reports_zero_balance_for_a_new_user(client):
    headers, _ = await _fresh_user(client)
    res = await client.get("/api/credits/me", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["balance"] == 0
    assert data["lifetime_granted"] == 0
    assert data["lifetime_consumed"] == 0
    assert data["enforced"] is False  # ENV=test 强制关闭


async def test_me_requires_auth(client):
    assert (await client.get("/api/credits/me")).status_code == 401


async def test_redeem_adds_credits_and_balance_reflects_it(client, admin_token):
    headers, _ = await _fresh_user(client)
    created = await _create_batch(client, admin_token, count=1, credits=750)
    code = created["codes"][0]

    res = await client.post("/api/credits/redeem", json={"code": code}, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["credits_added"] == 750
    assert body["balance"] == 750

    me = (await client.get("/api/credits/me", headers=headers)).json()
    assert me["balance"] == 750
    assert me["lifetime_granted"] == 750


async def test_redeem_twice_returns_409_with_code(client, admin_token):
    headers, _ = await _fresh_user(client)
    created = await _create_batch(client, admin_token, count=1, credits=100)
    code = created["codes"][0]
    await client.post("/api/credits/redeem", json={"code": code}, headers=headers)

    res = await client.post("/api/credits/redeem", json={"code": code}, headers=headers)
    assert res.status_code == 409
    assert res.json()["code"] == "redeem_code_used"


async def test_redeem_unknown_code_returns_404_with_code(client):
    headers, _ = await _fresh_user(client)
    res = await client.post(
        "/api/credits/redeem", json={"code": "ZZZZ-ZZZZ-ZZZZ-ZZZZ"}, headers=headers
    )
    assert res.status_code == 404
    assert res.json()["code"] == "redeem_code_not_found"


async def test_redeem_requires_auth(client):
    res = await client.post("/api/credits/redeem", json={"code": "X"})
    assert res.status_code == 401


async def test_ledger_lists_entries_newest_first(client, admin_token):
    headers, _ = await _fresh_user(client)
    created = await _create_batch(client, admin_token, count=2, credits=300)
    for code in created["codes"]:
        await client.post("/api/credits/redeem", json={"code": code}, headers=headers)

    body = (await client.get("/api/credits/ledger", headers=headers)).json()
    entries = body["entries"]
    assert len(entries) == 2
    assert entries[0]["delta"] == 300
    assert entries[0]["balance_after"] == 600  # 最新一条在后面那条之后
    assert entries[0]["reason"] == "redeem"
    assert entries[1]["balance_after"] == 300
    assert body["next_cursor"] is None


async def test_ledger_paginates_without_overlap(client, admin_token):
    headers, _ = await _fresh_user(client)
    created = await _create_batch(client, admin_token, count=5, credits=10)
    for code in created["codes"]:
        await client.post("/api/credits/redeem", json={"code": code}, headers=headers)

    page1 = (await client.get("/api/credits/ledger?limit=2", headers=headers)).json()
    assert len(page1["entries"]) == 2
    assert page1["next_cursor"]

    page2 = (
        await client.get(
            f"/api/credits/ledger?limit=2&cursor={page1['next_cursor']}", headers=headers
        )
    ).json()
    ids = {e["id"] for e in page1["entries"]} | {e["id"] for e in page2["entries"]}
    assert len(ids) == 4  # 无重叠


# ---- 管理端 --------------------------------------------------------------- #

async def test_admin_endpoints_reject_non_admin(client, auth_token):
    for method, path in [
        ("post", "/api/admin/redeem-batches"),
        ("get", "/api/admin/redeem-batches"),
        ("get", "/api/admin/credits/accounts"),
        ("post", "/api/admin/credits/adjust"),
    ]:
        res = await getattr(client, method)(path, headers=auth_headers(auth_token))
        assert res.status_code == 403, f"{method} {path} -> {res.status_code}"


async def test_create_batch_returns_plaintext_codes_once(client, admin_token):
    created = await _create_batch(client, admin_token, count=3, credits=200)
    assert len(created["codes"]) == 3

    # 列表接口不再返回明文，只有前缀
    listed = (
        await client.get("/api/admin/redeem-batches", headers=auth_headers(admin_token))
    ).json()
    target = next(b for b in listed if b["batch"]["id"] == created["batch"]["id"])
    assert target["total"] == 3
    assert target["active"] == 3
    assert target["redeemed"] == 0

    codes = (
        await client.get(
            f"/api/admin/redeem-batches/{created['batch']['id']}/codes",
            headers=auth_headers(admin_token),
        )
    ).json()
    assert len(codes) == 3
    prefixes = {c["code_prefix"] for c in codes}
    assert prefixes == {c.replace("-", "")[:6] for c in created["codes"]}
    assert all("code_hash" not in c for c in codes)


async def test_create_batch_validates_credits(client, admin_token):
    res = await client.post(
        "/api/admin/redeem-batches",
        json={"name": "bad", "credits_per_code": 0, "count": 1},
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 400
    assert res.json()["code"] == "redeem_batch_invalid_credits"


async def test_void_batch_endpoint(client, admin_token):
    created = await _create_batch(client, admin_token, count=3, credits=100)
    res = await client.post(
        f"/api/admin/redeem-batches/{created['batch']['id']}/void",
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 200
    assert res.json()["voided"] == 3


async def test_admin_can_adjust_user_credits(client, admin_token):
    user_headers, user = await _fresh_user(client)

    res = await client.post(
        "/api/admin/credits/adjust",
        json={"user_id": user["id"], "delta": 1234, "note": "客服补偿"},
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 200, res.text
    assert res.json()["balance"] == 1234

    me = (await client.get("/api/credits/me", headers=user_headers)).json()
    assert me["balance"] == 1234


async def test_admin_can_deduct_with_a_negative_delta(client, admin_token):
    user_headers, user = await _fresh_user(client)
    admin_h = auth_headers(admin_token)
    await client.post(
        "/api/admin/credits/adjust",
        json={"user_id": user["id"], "delta": 1000},
        headers=admin_h,
    )
    res = await client.post(
        "/api/admin/credits/adjust",
        json={"user_id": user["id"], "delta": -300},
        headers=admin_h,
    )
    assert res.status_code == 200, res.text
    assert res.json()["balance"] == 700
    assert res.json()["lifetime_consumed"] == 300


async def test_adjust_rejects_zero_delta(client, admin_token):
    _, user = await _fresh_user(client)
    res = await client.post(
        "/api/admin/credits/adjust",
        json={"user_id": user["id"], "delta": 0},
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 400
    assert res.json()["code"] == "credit_adjust_zero"


async def test_adjust_unknown_user_returns_404(client, admin_token):
    res = await client.post(
        "/api/admin/credits/adjust",
        json={"user_id": str(uuid.uuid4()), "delta": 10},
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 404
    assert res.json()["code"] == "user_not_found"


async def test_adjust_emits_an_audit_event(client, admin_token, monkeypatch):
    """调分必须留审计 —— 这是它区别于直接改库的全部意义。

    这里断言的是**路由确实调用了 audit_service.log 且参数正确**，而不是去
    查 audit_events 表。原因：`audit_service.log` 用自己的会话
    （`app.db.AsyncSessionLocal`），而测试库是 conftest 用 StaticPool 另建的
    内存 SQLite，两者不是同一个库（且 `AUTO_CREATE_TABLES=false`，那个库里
    根本没有表）。log() 会静默吞掉失败，所以查表只会得到一个假的失败。
    """
    from app.services import audit_service

    calls: list[dict] = []

    async def _spy(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(audit_service, "log", _spy)

    _, user = await _fresh_user(client)
    await client.post(
        "/api/admin/credits/adjust",
        json={"user_id": user["id"], "delta": 77, "note": "测试审计"},
        headers=auth_headers(admin_token),
    )

    assert calls, "调分没有写审计事件"
    assert calls[0]["action"] == "credits:adjust"
    assert calls[0]["target"] == user["id"]
    assert calls[0]["detail"]["delta"] == 77
    assert calls[0]["detail"]["note"] == "测试审计"


async def test_void_batch_emits_an_audit_event(client, admin_token, monkeypatch):
    """作废是破坏性操作，同样必须留审计。"""
    from app.services import audit_service

    calls: list[dict] = []

    async def _spy(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(audit_service, "log", _spy)

    created = await _create_batch(client, admin_token, count=2, credits=100)
    await client.post(
        f"/api/admin/redeem-batches/{created['batch']['id']}/void",
        headers=auth_headers(admin_token),
    )

    assert calls and calls[0]["action"] == "credits:void_batch"
    assert calls[0]["detail"]["voided"] == 2


async def test_admin_accounts_listing_shows_the_users_balance(client, admin_token):
    _, user = await _fresh_user(client)
    await client.post(
        "/api/admin/credits/adjust",
        json={"user_id": user["id"], "delta": 55},
        headers=auth_headers(admin_token),
    )
    rows = (
        await client.get("/api/admin/credits/accounts", headers=auth_headers(admin_token))
    ).json()
    target = next(r for r in rows if r["user_id"] == user["id"])
    assert target["balance"] == 55


async def test_admin_can_search_accounts_by_email(client, admin_token):
    _, user = await _fresh_user(client)
    rows = (
        await client.get(
            f"/api/admin/credits/accounts?search={user['email']}",
            headers=auth_headers(admin_token),
        )
    ).json()
    assert len(rows) == 1
    assert rows[0]["user_id"] == user["id"]
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && python -m pytest tests/test_credits_api.py -v
```

Expected: FAIL —— 404（路由未注册）

- [ ] **Step 3: 写 `app/schemas/credit.py`**

```python
"""积分与兑换码的请求 / 响应 DTO。"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class CreditAccountOut(BaseModel):
    balance: int
    lifetime_granted: int
    lifetime_consumed: int
    # 观察模式开关，供前端展示"当前不拦截"的提示。
    enforced: bool


class RedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class RedeemResultOut(BaseModel):
    credits_added: int
    balance: int
    batch_name: str


class LedgerEntryOut(ORMModel):
    id: uuid.UUID
    delta: int
    balance_after: int
    reason: str
    ref_type: str | None = None
    note: str | None = None
    created_at: datetime


class LedgerPageOut(BaseModel):
    entries: list[LedgerEntryOut]
    next_cursor: str | None = None


# ---- 管理端 --------------------------------------------------------------- #

class RedeemBatchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    credits_per_code: int = Field(gt=0)
    count: int = Field(gt=0)
    expires_at: datetime | None = None
    note: str | None = None


class RedeemBatchOut(ORMModel):
    id: uuid.UUID
    name: str
    credits_per_code: int
    expires_at: datetime | None = None
    note: str | None = None
    created_at: datetime


class RedeemBatchCreateOut(BaseModel):
    batch: RedeemBatchOut
    # 明文码，仅此一次。
    codes: list[str]


class RedeemBatchProgressOut(BaseModel):
    batch: RedeemBatchOut
    total: int
    redeemed: int
    void: int
    active: int


class RedeemCodeOut(ORMModel):
    id: uuid.UUID
    code_prefix: str
    status: str
    redeemed_by: uuid.UUID | None = None
    redeemed_at: datetime | None = None
    created_at: datetime


class VoidBatchOut(BaseModel):
    voided: int


class CreditAccountRowOut(BaseModel):
    user_id: uuid.UUID
    email: str
    username: str
    balance: int
    lifetime_granted: int
    lifetime_consumed: int


class CreditAdjustRequest(BaseModel):
    user_id: uuid.UUID
    # 非零；上限由 CREDITS_MAX_ADJUST 在服务层再校验一次。
    delta: int
    note: str | None = Field(default=None, max_length=500)
```

- [ ] **Step 4: 导出 schema**

在 `backend/app/schemas/__init__.py` 中加 import（按现有分组风格）：

```python
from app.schemas.credit import (
    CreditAccountOut,
    CreditAccountRowOut,
    CreditAdjustRequest,
    LedgerEntryOut,
    LedgerPageOut,
    RedeemBatchCreate,
    RedeemBatchCreateOut,
    RedeemBatchOut,
    RedeemBatchProgressOut,
    RedeemCodeOut,
    RedeemRequest,
    RedeemResultOut,
    VoidBatchOut,
)
```

并在 `__all__` 里追加同名条目。

- [ ] **Step 5: 写 `app/api/credits.py`**

```python
"""积分路由：用户侧兑换与余额，管理侧发码与调分。

两个 router 共用一个模块（与 ``app/api/memories.py`` 的
``router`` + ``user_router`` 同款约定），因为它们是同一个功能的两面。

所有业务失败都抛 :class:`AppException` —— 只有它会发出前端能解析的 ``code``
字段（裸 ``HTTPException`` 会被映射成 ``http_410`` 这类无意义的码，见
``app/core/exceptions.py`` 的 ``_STATUS_CODES``）。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.credits import get_credit_policy
from app.core.deps import get_current_admin, get_current_user
from app.core.exceptions import AppException
from app.core.rate_limit import rate_limit_user
from app.db import get_db
from app.models import User
from app.schemas import (
    CreditAccountOut,
    CreditAccountRowOut,
    CreditAdjustRequest,
    LedgerEntryOut,
    LedgerPageOut,
    RedeemBatchCreate,
    RedeemBatchCreateOut,
    RedeemBatchOut,
    RedeemBatchProgressOut,
    RedeemCodeOut,
    RedeemRequest,
    RedeemResultOut,
    VoidBatchOut,
)
from app.services import credit_service, redeem_service
from app.services.credit_service import CreditError

router = APIRouter(prefix="/api/credits", tags=["credits"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin-credits"])


def _to_app_exception(exc: CreditError) -> AppException:
    return AppException(exc.status_code, exc.code, exc.message)


# --------------------------------------------------------------------------- #
# 用户侧
# --------------------------------------------------------------------------- #
@router.get("/me", response_model=CreditAccountOut)
async def my_credits(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CreditAccountOut:
    """余额。账户行不存在时返回 0（不顺手创建，读接口不该有写副作用）。"""
    account = await credit_service.read_account(db, user.id)
    return CreditAccountOut(
        balance=int(account.balance) if account else 0,
        lifetime_granted=int(account.lifetime_granted) if account else 0,
        lifetime_consumed=int(account.lifetime_consumed) if account else 0,
        enforced=get_credit_policy().enforced,
    )


@router.post(
    "/redeem",
    response_model=RedeemResultOut,
    dependencies=[Depends(rate_limit_user(10, 60, "credits-redeem"))],
)
async def redeem(
    payload: RedeemRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedeemResultOut:
    try:
        result = await redeem_service.redeem(db, user_id=user.id, raw_code=payload.code)
    except CreditError as exc:
        raise _to_app_exception(exc)
    # 服务层只 flush，提交归路由（见 redeem_service 的事务边界约定）。
    await db.commit()
    return RedeemResultOut(
        credits_added=result.credits_added,
        balance=result.balance,
        batch_name=result.batch_name,
    )


@router.get("/ledger", response_model=LedgerPageOut)
async def my_ledger(
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LedgerPageOut:
    rows, next_cursor = await credit_service.ledger_page(
        db, user.id, limit=limit, cursor=cursor
    )
    return LedgerPageOut(
        entries=[LedgerEntryOut.model_validate(r) for r in rows],
        next_cursor=next_cursor,
    )


# --------------------------------------------------------------------------- #
# 管理侧
# --------------------------------------------------------------------------- #
@admin_router.post("/redeem-batches", response_model=RedeemBatchCreateOut)
async def create_redeem_batch(
    payload: RedeemBatchCreate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> RedeemBatchCreateOut:
    """生成一批兑换码。

    响应里的 ``codes`` 是明文，**只在这一次返回**。库里只写 SHA-256 哈希，
    此后再无法取回明文 —— 管理员必须当场导出。
    """
    try:
        batch, codes = await redeem_service.create_batch(
            db,
            admin_id=admin.id,
            name=payload.name,
            credits_per_code=payload.credits_per_code,
            count=payload.count,
            expires_at=payload.expires_at,
            note=payload.note,
        )
    except CreditError as exc:
        raise _to_app_exception(exc)
    await db.commit()
    await db.refresh(batch)
    return RedeemBatchCreateOut(
        batch=RedeemBatchOut.model_validate(batch), codes=codes
    )


@admin_router.get("/redeem-batches", response_model=list[RedeemBatchProgressOut])
async def list_redeem_batches(
    limit: int = Query(default=100, ge=1, le=500),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> list[RedeemBatchProgressOut]:
    rows = await redeem_service.list_batches(db, limit=limit)
    return [
        RedeemBatchProgressOut(
            batch=RedeemBatchOut.model_validate(row.batch),
            total=row.total,
            redeemed=row.redeemed,
            void=row.void,
            active=row.active,
        )
        for row in rows
    ]


@admin_router.get("/redeem-batches/{batch_id}/codes", response_model=list[RedeemCodeOut])
async def list_redeem_codes(
    batch_id: uuid.UUID,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> list[RedeemCodeOut]:
    """批次内码列表。只返回 6 位前缀，绝不含哈希或明文。"""
    rows = await redeem_service.list_codes(
        db, batch_id=batch_id, limit=limit, offset=offset
    )
    return [RedeemCodeOut.model_validate(r) for r in rows]


@admin_router.post("/redeem-batches/{batch_id}/void", response_model=VoidBatchOut)
async def void_redeem_batch(
    batch_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> VoidBatchOut:
    """作废该批所有未使用的码。已兑换的不受影响（分已经到用户账上了）。"""
    voided = await redeem_service.void_batch(db, batch_id=batch_id)
    await db.commit()

    # 作废是破坏性操作，留审计。best-effort，用自己的会话。
    from app.services import audit_service

    await audit_service.log(
        actor_id=admin.id,
        action="credits:void_batch",
        target=str(batch_id),
        detail={"voided": voided},
    )
    return VoidBatchOut(voided=voided)


@admin_router.get("/credits/accounts", response_model=list[CreditAccountRowOut])
async def list_credit_accounts(
    search: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> list[CreditAccountRowOut]:
    rows = await credit_service.list_accounts(
        db, search=search, limit=limit, offset=offset
    )
    return [
        CreditAccountRowOut(
            user_id=user.id,
            email=user.email,
            username=user.username,
            balance=int(account.balance) if account else 0,
            lifetime_granted=int(account.lifetime_granted) if account else 0,
            lifetime_consumed=int(account.lifetime_consumed) if account else 0,
        )
        for user, account in rows
    ]


@admin_router.post("/credits/adjust", response_model=CreditAccountRowOut)
async def adjust_credits(
    payload: CreditAdjustRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> CreditAccountRowOut:
    """手动加 / 扣积分。走同一套账本，并留下审计事件。"""
    target = (
        await db.execute(select(User).where(User.id == payload.user_id))
    ).scalar_one_or_none()
    if target is None:
        raise AppException(404, "user_not_found", "用户不存在")

    try:
        await credit_service.adjust(
            db,
            payload.user_id,
            delta=payload.delta,
            actor_id=admin.id,
            note=payload.note,
        )
    except CreditError as exc:
        raise _to_app_exception(exc)
    account = await credit_service.read_account(db, payload.user_id)
    await db.commit()

    # 审计：best-effort，用自己的会话，失败不影响调分结果。
    from app.services import audit_service

    await audit_service.log(
        actor_id=admin.id,
        action="credits:adjust",
        target=str(payload.user_id),
        detail={"delta": payload.delta, "note": payload.note},
    )

    return CreditAccountRowOut(
        user_id=target.id,
        email=target.email,
        username=target.username,
        balance=int(account.balance) if account else 0,
        lifetime_granted=int(account.lifetime_granted) if account else 0,
        lifetime_consumed=int(account.lifetime_consumed) if account else 0,
    )
```

- [ ] **Step 6: 挂载路由**

在 `backend/app/main.py` 的 `app.include_router(wechat.router)` 之后加：

```python
    app.include_router(credits.router)
    app.include_router(credits.admin_router)
```

并在文件顶部的 router import 区加 `credits`（与 `wechat` 同一行风格）。

- [ ] **Step 7: 运行测试确认通过**

```bash
cd backend && python -m pytest tests/test_credits_api.py -v
```

Expected: PASS —— 全部用例

- [ ] **Step 8: 跑一遍完整后端测试，确认没有回归**

```bash
cd backend && python -m pytest tests/ -q
```

Expected: 除已知的环境相关失败（`test_agent_phase2` / `test_agent_phase5` 需要真实模型端点）与已知的 `test_multi_agent_approval_pauses_then_resumes` 死锁外，其余全部通过

- [ ] **Step 9: 提交**

```bash
git add backend/app/schemas/credit.py backend/app/schemas/__init__.py backend/app/api/credits.py backend/app/main.py backend/tests/test_credits_api.py
git commit -m "feat(credits): 积分与兑换码 API

用户侧 /api/credits：余额、兑换、流水（游标分页）
管理侧 /api/admin：发码批次、批次进度、码列表、作废、余额列表、调分

错误一律走 AppException —— 裸 HTTPException 会被映射成 http_410 这类
无意义的 code，前端无法区分已过期与已作废。

兑换挂 rate_limit_user(10, 60)，与代码库其余限流一样用字面量。"
```

---

### Task 6: 统一结算入口 —— 修掉错误轮次不计费的漏洞

**Files:**
- Modify: `backend/app/services/chat_service.py`（5 处调用点 + 新增 `settle_turn_usage`）
- Test: `backend/tests/test_credits_settlement.py`

**Interfaces:**
- Consumes: `app.services.credit_service.charge_message_credits`（Task 3）
- Produces:
  - `async def settle_turn_usage(db, user_id, message, model_name, usage) -> None`（模块级函数）
  - `ChatService._finalize_error(..., user_id: uuid.UUID | None = None)`（新增关键字参数）
  - `ChatService._finalize_interrupted(..., user_id: uuid.UUID | None = None)`（新增关键字参数）

**为什么必须做这一步：** 现在 `_finalize_error`（`:1911`）与 `_finalize_interrupted`（`:1928`）调用 `_apply_usage_accounting` 记账，却不调用 `_charge_quota_if_enabled` 计费。今天这是配额旁路；接入积分后就是**故意让请求报错即可免费烧 token**。把记账与计费合并成单一入口后，结构上不再可能只记账不计费。

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_credits_settlement.py`：

```python
"""结算入口测试：错误轮次也必须扣分。"""
from __future__ import annotations

import uuid

import pytest

from app.credits import CreditPolicy, set_credit_policy
from app.models import Message
from app.services import credit_service
from app.services.chat_service import settle_turn_usage


@pytest.fixture(autouse=True)
def _priced(monkeypatch):
    set_credit_policy(CreditPolicy(enforced=False, credits_per_usd=1000.0))
    yield
    set_credit_policy(None)


async def _funded_user(db, amount=10_000):
    uid = uuid.uuid4()
    await credit_service.grant(db, uid, amount=amount, reason="admin_adjust")
    return uid


def _assistant_message(cost_usd, total_tokens=200):
    return Message(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        role="assistant",
        content="partial",
        metadata_={},
        model_name="gpt-4o",
        prompt_tokens=total_tokens // 2,
        completion_tokens=total_tokens // 2,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
    )


async def test_settle_charges_credits_for_a_normal_turn(db_session):
    uid = await _funded_user(db_session)
    msg = _assistant_message(0.02)
    await settle_turn_usage(db_session, uid, msg, "gpt-4o", {"prompt_tokens": 100, "completion_tokens": 100})
    account = await credit_service.read_account(db_session, uid)
    assert account.balance == 10_000 - 20  # 0.02 * 1000


async def test_settle_records_usage_fields_from_the_provider_payload(db_session):
    """记账那一半不能被这次重构破坏。"""
    uid = await _funded_user(db_session)
    msg = _assistant_message(None, total_tokens=0)
    await settle_turn_usage(
        db_session, uid, msg, "gpt-4o",
        {"prompt_tokens": 1234, "completion_tokens": 66},
    )
    assert msg.prompt_tokens == 1234
    assert msg.completion_tokens == 66
    assert msg.total_tokens == 1300


async def test_settle_charges_an_error_turn_that_consumed_tokens(db_session):
    """本任务的核心：以 error 收尾但 provider 报了 usage 的一轮必须扣分。

    修复前 _finalize_error 只记账不计费 —— 接入积分后就是"报错即免费"。
    """
    uid = await _funded_user(db_session)
    msg = _assistant_message(0.05)
    # 模拟 _finalize_error 的调用形态
    from app.services.chat_service import ChatService

    svc = ChatService()
    await svc._finalize_error(
        db_session,
        msg,
        "upstream blew up",
        finish_reason="error",
        code="provider_error",
        usage={"prompt_tokens": 100, "completion_tokens": 100, "cost_usd": 0.05},
        model_name="gpt-4o",
        user_id=uid,
    )
    account = await credit_service.read_account(db_session, uid)
    assert account.balance == 10_000 - 50


async def test_settle_charges_an_interrupted_turn(db_session):
    """客户端断连的一轮同样消耗了 token，必须扣分。"""
    uid = await _funded_user(db_session)
    msg = _assistant_message(0.03)
    from app.services.chat_service import ChatService

    await ChatService()._finalize_interrupted(
        db_session,
        msg,
        finish_reason="stream_disconnected",
        usage={"prompt_tokens": 100, "completion_tokens": 100, "cost_usd": 0.03},
        model_name="gpt-4o",
        user_id=uid,
    )
    account = await credit_service.read_account(db_session, uid)
    assert account.balance == 10_000 - 30


async def test_settle_is_idempotent_for_the_same_message(db_session):
    uid = await _funded_user(db_session)
    msg = _assistant_message(0.02)
    usage = {"prompt_tokens": 100, "completion_tokens": 100, "cost_usd": 0.02}
    await settle_turn_usage(db_session, uid, msg, "gpt-4o", usage)
    await settle_turn_usage(db_session, uid, msg, "gpt-4o", usage)
    account = await credit_service.read_account(db_session, uid)
    assert account.balance == 10_000 - 20  # 只扣一次


async def test_settle_does_not_charge_when_there_was_no_usage(db_session):
    uid = await _funded_user(db_session)
    msg = _assistant_message(None, total_tokens=0)
    msg.prompt_tokens = None
    msg.completion_tokens = None
    msg.total_tokens = None
    await settle_turn_usage(db_session, uid, msg, "gpt-4o", None)
    account = await credit_service.read_account(db_session, uid)
    assert account.balance == 10_000
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && python -m pytest tests/test_credits_settlement.py -v
```

Expected: FAIL —— `ImportError: cannot import name 'settle_turn_usage'`

- [ ] **Step 3: 新增 `settle_turn_usage`**

在 `backend/app/services/chat_service.py` 中，紧接 `_charge_quota_if_enabled` 之后（约 `:236`）插入：

```python
async def settle_turn_usage(
    db: AsyncSession,
    user_id: uuid.UUID,
    message: Message,
    model_name: str | None,
    usage: dict[str, Any] | None,
) -> None:
    """一轮对话的统一结算入口：记账 + 配额计费 + 积分扣减。

    五个调用点全部走这里，不再各自成对调用 ``_apply_usage_accounting`` 与
    ``_charge_quota_if_enabled``。原因是一个真实的漏洞：``_finalize_error``
    与 ``_finalize_interrupted`` 过去只记账不计费，于是"故意让请求报错"就能
    烧 token 而不付账。合并成单一入口后，结构上不可能只记账不计费。

    原子性边界：记账与积分扣减在**同一 DB 事务**内（由调用方提交），幂等由
    ``credit_ledger`` 上 ``ref_type='message'`` 的唯一部分索引保证。配额计费
    走 Redis，是 best-effort（与 :mod:`app.quotas` 既有语义一致），失败不影响
    积分账本 —— 积分是钱，配额是限流，可靠性要求不同，不该捆成一个事务。
    """
    _apply_usage_accounting(message, model_name, usage)
    await _charge_quota_if_enabled(str(user_id), message)
    await credit_service.charge_message_credits(db, user_id, message)
```

在文件顶部的 import 区加（与既有 `from app.quotas import ...` 同一区域）：

```python
from app.services import credit_service
```

- [ ] **Step 4: 替换内联路径的调用点**

`:1754` 附近，把：

```python
                    _apply_usage_accounting(
                        assistant_msg, cfg.model_name, evt.data.get("usage")
                    )
                    # Quota charge (Task 11): forward the SERVER-computed usage
                    # to the tenant's quota counters. No-op unless QUOTAS_ENABLED.
                    await _charge_quota_if_enabled(str(user.id), assistant_msg)
```

替换为：

```python
                    # 统一结算：记账 + 配额计费 + 积分扣减。
                    await settle_turn_usage(
                        db,
                        user.id,
                        assistant_msg,
                        cfg.model_name,
                        evt.data.get("usage"),
                    )
```

- [ ] **Step 5: 替换 durable 路径的两处调用点**

`:2504` 附近（done 分支）替换为：

```python
                await settle_turn_usage(
                    db,
                    user.id,
                    assistant_msg,
                    cfg.model_name,
                    evt.data.get("usage"),
                )
```

`:2537` 附近（CancelledError 分支）替换为：

```python
        await settle_turn_usage(
            db,
            user.id,
            assistant_msg,
            cfg.model_name,
            ctx.extra.get("usage"),
        )
```

- [ ] **Step 6: 给两个收尾函数加 `user_id` 参数并结算**

`_finalize_error` 签名改为（在 `budget` 之后加）：

```python
        budget: dict[str, Any] | None = None,
        user_id: uuid.UUID | None = None,
    ) -> None:
```

函数体的：

```python
        _apply_usage_accounting(assistant_msg, model_name, usage)
        await commit_with_rollback(db)
```

替换为：

```python
        if user_id is not None:
            await settle_turn_usage(db, user_id, assistant_msg, model_name, usage)
        else:
            # 没有 user 上下文（例如运维脚本直接调用）时退化为只记账，
            # 不静默漏计费 —— 记一条警告便于排查。
            logger.warning(
                "_finalize_error called without user_id; usage was recorded but not charged"
            )
            _apply_usage_accounting(assistant_msg, model_name, usage)
        await commit_with_rollback(db)
```

`_finalize_interrupted` 同样加 `user_id: uuid.UUID | None = None` 参数，体改为：

```python
        if user_id is not None:
            await settle_turn_usage(db, user_id, assistant_msg, model_name, usage)
        else:
            logger.warning(
                "_finalize_interrupted called without user_id; usage was recorded but not charged"
            )
            _apply_usage_accounting(assistant_msg, model_name, usage)
        await _persist_partial(db, assistant_msg)
```

- [ ] **Step 7: 更新三处调用点传参**

`:1807`（内联 `_finalize_error`）加 `user_id=user.id`：

```python
                        await self._finalize_error(
                            db, assistant_msg, err_msg,
                            finish_reason=err_finish, code=err_code,
                            usage=evt.data.get("usage"),
                            model_name=cfg.model_name,
                            budget=evt.data.get("budget"),
                            user_id=user.id,
                        )
```

`:1860`（内联 `_finalize_interrupted`）加 `user_id=user.id`：

```python
                await self._finalize_interrupted(
                    db,
                    assistant_msg,
                    finish_reason=reason,
                    usage=ctx.extra.get("usage"),
                    model_name=cfg.model_name,
                    user_id=user.id,
                )
```

`:2520`（durable `_finalize_error`）加 `user_id=user.id`。

- [ ] **Step 8: 运行测试确认通过**

```bash
cd backend && python -m pytest tests/test_credits_settlement.py -v
```

Expected: PASS —— 全部用例

- [ ] **Step 9: 确认没有破坏既有聊天与计费测试**

```bash
cd backend && python -m pytest tests/test_chat_stream.py tests/test_message_feedback.py tests/test_budget_integration.py tests/test_durable_execution.py -q
```

Expected: PASS。若有失败，检查是否漏改了一处 `_apply_usage_accounting` / `_charge_quota_if_enabled` 配对。

- [ ] **Step 10: 提交**

```bash
git add backend/app/services/chat_service.py backend/tests/test_credits_settlement.py
git commit -m "fix(credits): 统一结算入口，修掉错误轮次不计费的漏洞

_finalize_error 与 _finalize_interrupted 过去调 _apply_usage_accounting
记账却不调 _charge_quota_if_enabled 计费。今天这是配额旁路，接入积分后
就是"故意让请求报错即可免费烧 token"。

改为单一入口 settle_turn_usage，五个调用点全部走它，结构上不再可能
只记账不计费。没有 user 上下文的调用退化为只记账并记警告，不静默漏计费。"
```

---

### Task 7: 余额拦截与注册建账户

**Files:**
- Modify: `backend/app/services/chat_service.py`（`stream` 与 `create_and_enqueue_durable_run` 两处准入）
- Modify: `backend/app/api/auth.py`（注册时建账户 + 可选赠送；注意不是 auth_service.py，那里的 register 是死代码）
- Test: `backend/tests/test_credits_enforcement.py`

**Interfaces:**
- Consumes: `app.credits.get_credit_policy`、`app.services.credit_service`
- Produces:
  - `GET /api/credits/me` 的 `enforced` 字段在策略开启后为 `true`
  - 内联路径余额不足时 SSE 事件 `{"kind": "error", "data": {"code": "insufficient_credits", ...}}`
  - durable 路径余额不足时 HTTP 402 `insufficient_credits`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_credits_enforcement.py`：

```python
"""余额拦截测试：观察模式放行、强制模式拦截、两条执行路径都覆盖。

注意两条路径的入口**是同一个路由** `POST /api/chat/stream`：
`BACKGROUND_WORKER="inprocess"`（测试默认，见 conftest）走内联执行，
其余值走 durable 分发（见 `app/api/chat.py:197`）。所以用 monkeypatch 切。
"""
from __future__ import annotations

import uuid

import pytest

from app.core.config import get_settings
from app.credits import CreditPolicy, get_credit_policy, set_credit_policy
from app.services import credit_service
from tests.conftest import auth_headers


@pytest.fixture
def observing():
    """观察模式：扣分记账但不拦截。"""
    set_credit_policy(CreditPolicy(enforced=False))
    yield get_credit_policy()
    set_credit_policy(None)


@pytest.fixture
def enforcing():
    """强制模式：余额不足拒绝请求。"""
    set_credit_policy(CreditPolicy(enforced=True))
    yield get_credit_policy()
    set_credit_policy(None)


async def _create_mock_model(client, headers) -> str:
    """建一个 mock 模型配置，ChatRequest.model_id 需要它。"""
    r = await client.post(
        "/api/models",
        json={
            "name": "credits mock",
            "provider": "openai-compatible",
            "api_base_url": "http://localhost/v1",
            "model_name": "mock-model",
            "supports_stream": True,
            "supports_tools": True,
            "is_embedding": False,
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _fund(db, user_id, amount=10_000):
    await credit_service.grant(db, user_id, amount=amount, reason="admin_adjust")
    await db.commit()


async def test_observation_mode_reports_not_enforced(client, auth_token, observing):
    me = (await client.get("/api/credits/me", headers=auth_headers(auth_token))).json()
    assert me["enforced"] is False


async def test_enforcing_mode_reports_enforced(client, auth_token, enforcing):
    me = (await client.get("/api/credits/me", headers=auth_headers(auth_token))).json()
    assert me["enforced"] is True


# ---- 内联路径：SSE error 事件 ---------------------------------------------- #

async def test_inline_path_blocks_zero_balance_when_enforcing(
    client, auth_token, enforcing, offline_model
):
    h = auth_headers(auth_token)
    model_id = await _create_mock_model(client, h)

    async with client.stream(
        "POST",
        "/api/chat/stream",
        json={"content": "hi", "model_id": model_id},
        headers=h,
    ) as resp:
        assert resp.status_code == 200  # SSE 一旦开始就是 200
        body = "".join([chunk async for chunk in resp.aiter_text()])

    assert "insufficient_credits" in body
    assert "积分不足" in body


async def test_inline_path_allows_zero_balance_when_observing(
    client, auth_token, observing, offline_model
):
    h = auth_headers(auth_token)
    model_id = await _create_mock_model(client, h)

    async with client.stream(
        "POST",
        "/api/chat/stream",
        json={"content": "hi", "model_id": model_id},
        headers=h,
    ) as resp:
        body = "".join([chunk async for chunk in resp.aiter_text()])

    assert "insufficient_credits" not in body


# ---- durable 路径：HTTP 402 ------------------------------------------------ #

async def test_durable_path_blocks_zero_balance_before_creating_any_record(
    client, auth_token, enforcing, db_session, offline_model, monkeypatch
):
    """durable 分发在构造 StreamingResponse 之前抛 AppException，
    所以客户端拿到的是真正的 HTTP 402，而不是 SSE 里的错误帧。

    并且不得有任何副作用 —— 既不建 run，也不建会话。准入检查必须排在
    `_get_or_create_conversation` 前面，否则被拒的请求仍会留下一个空会话。
    """
    monkeypatch.setattr(get_settings(), "BACKGROUND_WORKER", "durable")
    h = auth_headers(auth_token)
    model_id = await _create_mock_model(client, h)

    from sqlalchemy import func, select

    from app.models import AgentRun, Conversation

    async def _counts():
        runs = (await db_session.execute(select(func.count()).select_from(AgentRun))).scalar_one()
        convs = (
            await db_session.execute(select(func.count()).select_from(Conversation))
        ).scalar_one()
        return runs, convs

    pre = await _counts()

    res = await client.post(
        "/api/chat/stream",
        json={"content": "hi", "model_id": model_id},
        headers=h,
    )
    assert res.status_code == 402, res.text
    assert res.json()["code"] == "insufficient_credits"

    assert await _counts() == pre, "被拦截时不得创建任何会话或 run"


async def test_durable_path_passes_when_funded(
    client, auth_token, enforcing, db_session, offline_model, monkeypatch
):
    """有余额时不得 402。只断言响应头就退出 —— 没有 worker 时后续事件永远不来。"""
    monkeypatch.setattr(get_settings(), "BACKGROUND_WORKER", "durable")
    h = auth_headers(auth_token)
    model_id = await _create_mock_model(client, h)
    await _fund(db_session, uuid.UUID("00000000-0000-0000-0000-000000000001"))

    async with client.stream(
        "POST",
        "/api/chat/stream",
        json={"content": "hi", "model_id": model_id},
        headers=h,
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers.get("x-durable-run-id") is not None


# ---- 注册建账户 ------------------------------------------------------------ #

async def test_registration_creates_a_zero_balance_account(client, db_session):
    """新注册用户必须有账户行 —— 否则"余额 0"和"账户不存在"变成两件事。"""
    res = await client.post(
        "/api/auth/register",
        json={
            "email": "newbie@example.com",
            "username": "newbie",
            "password": "NewbiePass123",
        },
    )
    assert res.status_code == 201, res.text
    new_id = uuid.UUID(res.json()["user"]["id"])

    account = await credit_service.read_account(db_session, new_id)
    assert account is not None
    assert account.balance == 0


async def test_registration_applies_signup_bonus_when_configured(client, db_session):
    from dataclasses import replace

    set_credit_policy(replace(CreditPolicy(), signup_bonus=888))
    try:
        res = await client.post(
            "/api/auth/register",
            json={
                "email": "bonus@example.com",
                "username": "bonususer",
                "password": "BonusPass123",
            },
        )
        assert res.status_code == 201, res.text
        new_id = uuid.UUID(res.json()["user"]["id"])
        account = await credit_service.read_account(db_session, new_id)
        assert account.balance == 888
    finally:
        set_credit_policy(None)
```

说明：

- `verification_code` **必须省略**而不是传 `""` —— `RegisterRequest` 上它是 `Field(min_length=6)`，传空串会 422。conftest 设了 `MAIL_ENABLED=false`，所以不带验证码是合法的。
- 注册返回 **201** 且体是 `TokenResponse`，用户信息在 `res.json()["user"]`。

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && python -m pytest tests/test_credits_enforcement.py -v
```

Expected: FAIL —— 402 断言失败（现在没有任何拦截）

- [ ] **Step 3: 内联路径的准入拦截**

在 `backend/app/services/chat_service.py` 的 `stream` 方法中，`admit_run` 之后、`self._run(...)` 之前插入：

```python
            # 积分准入：余额 <= 0 时在调用模型之前拒绝，避免已产生成本。
            # CREDITS_ENFORCED 关闭时只记账不拦截（观察模式）。
            credit_policy = get_credit_policy()
            if credit_policy.enforced:
                account = await credit_service.read_account(db, user.id)
                if account is None or int(account.balance) <= 0:
                    yield _event(
                        "error",
                        {
                            "code": "insufficient_credits",
                            "message": "积分不足，请先兑换后再继续对话",
                            "balance": int(account.balance) if account else 0,
                        },
                    )
                    return
```

在 import 区加（与 `from app.quotas import ...` 同区域）：

```python
from app.credits import get_credit_policy
```

- [ ] **Step 4: durable 路径的准入拦截**

在 `create_and_enqueue_durable_run` 的方法体**最开头**插入（即
`conversation = await _get_or_create_conversation(...)` **之前**）：

```python
        # 积分准入：与内联路径同款规则，但不走 SSE —— 这个端点返回 run_id，
        # 所以用 HTTP 状态码拒绝。必须放在最前面：_get_or_create_conversation
        # 会真的建一个会话行，拦在它后面就"已经产生了副作用"。
        credit_policy = get_credit_policy()
        if credit_policy.enforced:
            account = await credit_service.read_account(db, user.id)
            if account is None or int(account.balance) <= 0:
                from app.core.exceptions import AppException as _AppException

                raise _AppException(
                    402,
                    "insufficient_credits",
                    "积分不足，请先兑换后再继续对话",
                    {"balance": int(account.balance) if account else 0},
                )
```

- [ ] **Step 5: 注册时创建账户行**

**注意目标文件是 `backend/app/api/auth.py` 而不是 `app/services/auth_service.py`。**
注册逻辑实际写在路由里（`app/api/auth.py:71` 起的 `register`）；`auth_service.register`
虽然存在但**没有任何调用方**，是死代码，改它不会有任何效果。

在 `app/api/auth.py` 的 `register` 函数中，`await db.refresh(user)` 之后、
`await audit_service.log(...)` 之前插入：

```python
    # 积分账户：注册即建行（余额 0，或配置的注册赠送）。不建的话
    # "余额为 0" 和 "账户不存在" 在排查时会变成两件事。
    from app.credits import get_credit_policy
    from app.services import credit_service

    await credit_service.get_or_create_account(db, user.id)
    _credit_policy = get_credit_policy()
    if _credit_policy.signup_bonus > 0:
        await credit_service.grant(
            db,
            user.id,
            amount=_credit_policy.signup_bonus,
            reason="signup_bonus",
            ref_type="user",
            ref_id=str(user.id),
            note="注册赠送",
        )
    await db.commit()
```

要点：

- `get_or_create_account` 内部用 `db.begin_nested()` 开 SAVEPOINT，嵌在路由已有的
  事务里安全。
- 注册赠送用 `ref_type="user"` + `reason="signup_bonus"` —— 唯一部分索引保证同一
  用户永远不会被赠送两次，即使注册流程被重试。
- 这一次 `await db.commit()` 是必要的：`credit_service.grant` 不提交（它要参与调用方
  的事务），而这里账户创建就该在返回 token 之前落库。

- [ ] **Step 6: 运行测试确认通过**

```bash
cd backend && python -m pytest tests/test_credits_enforcement.py -v
```

Expected: PASS —— 全部用例

- [ ] **Step 7: 确认没有破坏注册与聊天测试**

```bash
cd backend && python -m pytest tests/test_auth.py tests/test_chat_stream.py tests/test_durable_dispatch.py -q
```

Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add backend/app/services/chat_service.py backend/app/api/auth.py backend/tests/test_credits_enforcement.py
git commit -m "feat(credits): 余额拦截（观察模式可控）与注册建账户

两条路径的入口是同一个路由 POST /api/chat/stream：BACKGROUND_WORKER 为
inprocess 走内联（SSE error 事件），否则走 durable 分发（在构造流式响应
之前抛 AppException，所以客户端拿到真正的 HTTP 402）。

CREDITS_ENFORCED 默认关（观察模式）：扣分照常记账、余额照常显示，
但不拦截。上线时先发码核对扣分数字，再打开拦截，避免上线当天把
所有余额为 0 的老用户锁在外面。

注册建账户 + 可选赠送写在 app/api/auth.py 的路由里（auth_service.register
没有调用方，是死代码）；赠送用 ref_type='user' 的唯一约束兜底，重试不会双重赠送。"
```

---

### Task 8: 前端数据层

**Files:**
- Create: `frontend/src/lib/credits.ts`（纯函数 + 文案映射）
- Create: `frontend/src/lib/__tests__/credits.test.ts`
- Create: `frontend/src/hooks/useCredits.ts`
- Modify: `frontend/src/lib/types.ts`
- Modify: `frontend/src/lib/api.ts`

**Interfaces:**
- Consumes: Task 5 的 API 契约
- Produces:
  - `normalizeRedeemCodeInput(raw: string): string`（展示用格式化）
  - `formatCredits(n: number | null | undefined): string`
  - `REDEEM_ERROR_MESSAGES: Record<string, string>` / `redeemErrorMessage(code: string, fallback: string): string`
  - `useCredits(): { credits, isLoading, isError }`（React Query，key `["credits","me"]`）
  - `api.fetchCredits()` / `api.redeemCode(code)` / `api.fetchCreditLedger(limit, cursor)`
  - `api.adminListRedeemBatches()` / `api.adminCreateRedeemBatch(body)` / `api.adminListRedeemCodes(batchId)` / `api.adminVoidRedeemBatch(batchId)` / `api.adminListCreditAccounts(search)` / `api.adminAdjustCredits(body)`
  - 类型 `CreditAccountInfo`、`CreditLedgerEntry`、`CreditLedgerPage`、`RedeemBatchInfo`、`RedeemBatchProgress`、`RedeemCodeInfo`、`CreditAccountRow`

- [ ] **Step 1: 写失败测试**

创建 `frontend/src/lib/__tests__/credits.test.ts`：

```ts
import { describe, expect, it } from "vitest";

import {
  REDEEM_ERROR_MESSAGES,
  formatCredits,
  normalizeRedeemCodeInput,
  redeemErrorMessage,
} from "@/lib/credits";

describe("normalizeRedeemCodeInput", () => {
  it("大写并把字母数字分组为四组四位", () => {
    expect(normalizeRedeemCodeInput("ab12cd34ef56gh78")).toBe("AB12-CD34-EF56-GH78");
  });

  it("丢弃用户粘贴进来的分隔符与空白", () => {
    expect(normalizeRedeemCodeInput(" ab12 cd34-ef56_gh78 ")).toBe("AB12-CD34-EF56-GH78");
  });

  it("修正手抄歧义字符 I/L→1、O→0", () => {
    expect(normalizeRedeemCodeInput("oooo1111lllloo11")).toBe("0000-1111-1111-0011");
  });

  it("超过 16 位时截断，不无限增长", () => {
    const out = normalizeRedeemCodeInput("a".repeat(40));
    expect(out.replace(/-/g, "")).toHaveLength(16);
  });

  it("不足一组时不补分隔符", () => {
    expect(normalizeRedeemCodeInput("ab1")).toBe("AB1");
  });

  it("空输入返回空", () => {
    expect(normalizeRedeemCodeInput("")).toBe("");
    expect(normalizeRedeemCodeInput("   ")).toBe("");
  });
});

describe("formatCredits", () => {
  it("负余额显示为 0（最后一轮透支在用户侧不应显示成欠款）", () => {
    expect(formatCredits(-15)).toBe("0");
    expect(formatCredits(-1)).toBe("0");
  });

  it("null / undefined 显示为 0", () => {
    expect(formatCredits(null)).toBe("0");
    expect(formatCredits(undefined)).toBe("0");
  });

  it("正数带千分位", () => {
    expect(formatCredits(0)).toBe("0");
    expect(formatCredits(1234)).toBe("1,234");
    expect(formatCredits(1000000)).toBe("1,000,000");
  });
});

describe("redeemErrorMessage", () => {
  it("每个后端错误码都有中文文案", () => {
    for (const code of [
      "redeem_code_not_found",
      "redeem_code_used",
      "redeem_code_expired",
      "redeem_code_void",
    ]) {
      expect(REDEEM_ERROR_MESSAGES[code]).toBeTruthy();
    }
  });

  it("未知码回落到后端给的 message", () => {
    expect(redeemErrorMessage("something_new", "后端说的一句话")).toBe("后端说的一句话");
  });

  it("未知码且没有 message 时给通用文案", () => {
    expect(redeemErrorMessage("something_new", "")).toBe("兑换失败，请稍后重试");
  });

  it("已知码优先用映射文案而不是后端 message", () => {
    expect(redeemErrorMessage("redeem_code_used", "raw")).toContain("已被使用");
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd frontend && npx vitest run src/lib/__tests__/credits.test.ts
```

Expected: FAIL —— `Failed to resolve import "@/lib/credits"`

- [ ] **Step 3: 写 `frontend/src/lib/credits.ts`**

```ts
/**
 * 积分相关的纯函数与文案映射。
 *
 * 与后端 `app/credits.py` 的规范化规则必须保持一致：去掉非字母数字、
 * 转大写、修正手抄歧义字符（I/L→1、O→0）。两边不一致会导致用户明明输对了
 * 却兑换失败。
 */

const CODE_LENGTH = 16;
const CODE_GROUP = 4;

// 与后端 _CHAR_FIXES 一致。
const CHAR_FIXES: Record<string, string> = { I: "1", L: "1", O: "0" };

/**
 * 把输入框里的内容整理成展示格式（XXXX-XXXX-XXXX-XXXX）。
 *
 * 在 onChange 里直接调用，用户粘贴带空格 / 小写 / 别的分隔符的码时会被
 * 悄悄修正，减少"我明明输对了"的客服成本。
 */
export function normalizeRedeemCodeInput(raw: string): string {
  const chars: string[] = [];
  for (const ch of (raw || "").toUpperCase()) {
    if (!/[A-Z0-9]/.test(ch)) continue;
    chars.push(CHAR_FIXES[ch] ?? ch);
    if (chars.length >= CODE_LENGTH) break;
  }
  const groups: string[] = [];
  for (let i = 0; i < chars.length; i += CODE_GROUP) {
    groups.push(chars.slice(i, i + CODE_GROUP).join(""));
  }
  return groups.join("-");
}

/**
 * 余额展示。
 *
 * 负数显示为 0：准入在轮前、扣费在轮后，最后一轮会把余额扣成负数，那是
 * 平台的负债而不是用户的欠款，在用户侧显示成负数只会造成困惑。
 */
export function formatCredits(value: number | null | undefined): string {
  const safe = Math.max(0, Math.floor(Number(value) || 0));
  return safe.toLocaleString("zh-CN");
}

/** 后端稳定错误码 → 用户可读文案。 */
export const REDEEM_ERROR_MESSAGES: Record<string, string> = {
  redeem_code_not_found: "兑换码不存在，请检查是否输入有误",
  redeem_code_used: "该兑换码已被使用",
  redeem_code_expired: "该兑换码已过期",
  redeem_code_void: "该兑换码已作废",
  rate_limited: "操作过于频繁，请稍后再试",
};

/** 已知码用映射文案；未知码回落到后端 message，再兜底通用文案。 */
export function redeemErrorMessage(code: string, fallback?: string): string {
  return REDEEM_ERROR_MESSAGES[code] || fallback || "兑换失败，请稍后重试";
}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd frontend && npx vitest run src/lib/__tests__/credits.test.ts
```

Expected: PASS —— 全部用例

- [ ] **Step 5: 加类型**

在 `frontend/src/lib/types.ts` 末尾追加：

```ts
// ===========================================================================
// Credits / redeem codes
// ===========================================================================

export interface CreditAccountInfo {
  balance: number;
  lifetime_granted: number;
  lifetime_consumed: number;
  /** 观察模式开关：false 表示余额不足不会拦截。 */
  enforced: boolean;
}

export interface CreditLedgerEntry {
  id: string;
  delta: number;
  balance_after: number;
  reason: "redeem" | "admin_adjust" | "usage" | "signup_bonus" | string;
  ref_type: string | null;
  note: string | null;
  created_at: string;
}

export interface CreditLedgerPage {
  entries: CreditLedgerEntry[];
  next_cursor: string | null;
}

export interface RedeemResult {
  credits_added: number;
  balance: number;
  batch_name: string;
}

export interface RedeemBatchInfo {
  id: string;
  name: string;
  credits_per_code: number;
  expires_at: string | null;
  note: string | null;
  created_at: string;
}

export interface RedeemBatchProgress {
  batch: RedeemBatchInfo;
  total: number;
  redeemed: number;
  void: number;
  active: number;
}

export interface RedeemBatchCreateResult {
  batch: RedeemBatchInfo;
  /** 明文码，仅创建响应返回一次。 */
  codes: string[];
}

export interface RedeemCodeInfo {
  id: string;
  code_prefix: string;
  status: "active" | "redeemed" | "void" | string;
  redeemed_by: string | null;
  redeemed_at: string | null;
  created_at: string;
}

export interface CreditAccountRow {
  user_id: string;
  email: string;
  username: string;
  balance: number;
  lifetime_granted: number;
  lifetime_consumed: number;
}
```

- [ ] **Step 6: 加 API 方法**

在 `frontend/src/lib/api.ts` 顶部的类型 import 列表中加入 `CreditAccountInfo`、`CreditAccountRow`、`CreditLedgerPage`、`RedeemBatchCreateResult`、`RedeemBatchProgress`、`RedeemCodeInfo`、`RedeemResult`。

在 `api` 对象内追加（放在已有 `adminAuditLog` 附近保持管理端方法聚集）：

```ts
  // ---- Credits（积分） ----
  fetchCredits: () => request<CreditAccountInfo>("GET", "/api/credits/me"),

  redeemCode: (code: string) =>
    request<RedeemResult>("POST", "/api/credits/redeem", { code }),

  fetchCreditLedger: (limit = 50, cursor?: string | null) => {
    const qs = new URLSearchParams({ limit: String(limit) });
    if (cursor) qs.set("cursor", cursor);
    return request<CreditLedgerPage>("GET", `/api/credits/ledger?${qs.toString()}`);
  },

  // ---- Credits（管理端） ----
  adminListRedeemBatches: () =>
    request<RedeemBatchProgress[]>("GET", "/api/admin/redeem-batches"),

  adminCreateRedeemBatch: (body: {
    name: string;
    credits_per_code: number;
    count: number;
    expires_at?: string | null;
    note?: string | null;
  }) => request<RedeemBatchCreateResult>("POST", "/api/admin/redeem-batches", body),

  adminListRedeemCodes: (batchId: string, limit = 200, offset = 0) =>
    request<RedeemCodeInfo[]>(
      "GET",
      `/api/admin/redeem-batches/${batchId}/codes?limit=${limit}&offset=${offset}`
    ),

  adminVoidRedeemBatch: (batchId: string) =>
    request<{ voided: number }>(
      "POST",
      `/api/admin/redeem-batches/${batchId}/void`
    ),

  adminListCreditAccounts: (search?: string) => {
    const qs = new URLSearchParams();
    if (search) qs.set("search", search);
    return request<CreditAccountRow[]>(
      "GET",
      `/api/admin/credits/accounts${qs.toString() ? `?${qs.toString()}` : ""}`
    );
  },

  adminAdjustCredits: (body: { user_id: string; delta: number; note?: string | null }) =>
    request<CreditAccountRow>("POST", "/api/admin/credits/adjust", body),
```

- [ ] **Step 7: 写 `frontend/src/hooks/useCredits.ts`**

```ts
"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { useAuth } from "@/hooks/useAuth";

/**
 * 当前用户的积分余额。
 *
 * 侧边栏与积分页共用这一份 React Query 缓存（key `["credits","me"]`），
 * 兑换或调分后失效该 key 即可让两处同时刷新。
 *
 * `enabled` 跟着登录态走：未登录时不发请求（侧边栏在登录页也会渲染）。
 * 不引入 zustand —— 代码库里已有 selector 返回 `?? []` 导致无限重渲染的
 * 前车之鉴，余额这种服务端状态本来就该归 React Query。
 */
export function useCredits() {
  const { user, isLoading: authLoading } = useAuth();

  const query = useQuery({
    queryKey: ["credits", "me"],
    queryFn: api.fetchCredits,
    enabled: !!user && !authLoading,
    // 余额变化不需要秒级同步，避免每次窗口聚焦都打一次接口。
    staleTime: 30_000,
  });

  return {
    credits: query.data,
    isLoading: query.isLoading,
    isError: query.isError,
  };
}
```

- [ ] **Step 8: 类型检查与测试**

```bash
cd frontend && npx tsc --noEmit && npx vitest run src/lib/__tests__/credits.test.ts
```

Expected: 无类型错误；测试 PASS

- [ ] **Step 9: 提交**

```bash
git add frontend/src/lib/credits.ts frontend/src/lib/__tests__/credits.test.ts frontend/src/lib/types.ts frontend/src/lib/api.ts frontend/src/hooks/useCredits.ts
git commit -m "feat(credits): 前端数据层

纯函数（兑换码输入规范化、余额格式化、错误码文案）+ 类型 + api 方法 +
useCredits hook。

输入规范化与后端 app/credits.py 的规则必须一致，两边不一致会导致用户
明明输对了却兑换失败。

余额走 React Query 而非新增 zustand store；未登录时不发请求。"
```

---

### Task 9: 用户侧界面 —— 积分页与侧边栏余额

**Files:**
- Create: `frontend/src/app/settings/credits/page.tsx`
- Modify: `frontend/src/app/settings/layout.tsx`（NAV 新增一项）
- Modify: `frontend/src/components/sidebar.tsx`（常驻余额）
- Test: `frontend/src/lib/__tests__/credits.test.ts`（复用 Task 8 的纯函数测试）

**Interfaces:**
- Consumes: Task 8 的 `useCredits`、`api.fetchCreditLedger`、`api.redeemCode`、`normalizeRedeemCodeInput`、`formatCredits`、`redeemErrorMessage`
- Produces: 路由 `/settings/credits`；侧边栏余额入口

- [ ] **Step 1: 加设置导航项**

在 `frontend/src/app/settings/layout.tsx` 的 `NAV` 数组里，`账号安全` 之前插入：

```ts
  {
    label: "积分",
    href: "/settings/credits",
    icon: Coins,
    description: "兑换码、余额与消费明细",
  },
```

并在文件顶部 `lucide-react` 的 import 中加入 `Coins`。

- [ ] **Step 2: 写积分页**

创建 `frontend/src/app/settings/credits/page.tsx`：

```tsx
"use client";

import { useState } from "react";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Coins, Info } from "lucide-react";

import { api, ApiError } from "@/lib/api";
import { formatCredits, normalizeRedeemCodeInput, redeemErrorMessage } from "@/lib/credits";
import { useCredits } from "@/hooks/useCredits";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

const LEDGER_KEY = ["credits", "ledger"] as const;

/** 流水原因 → 中文标签。 */
const REASON_LABELS: Record<string, string> = {
  redeem: "兑换码",
  admin_adjust: "管理员调整",
  usage: "对话消耗",
  signup_bonus: "注册赠送",
};

/**
 * 积分页：余额、兑换码输入、消费流水。
 *
 * 观察模式下顶部会显示提示条 —— 此时余额不足不会拦截，用户看到扣分却
 * 没被拦会困惑，必须说清楚。
 */
export default function CreditsSettingsPage() {
  const qc = useQueryClient();
  const { credits, isLoading } = useCredits();
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);

  const ledgerQ = useInfiniteQuery({
    queryKey: LEDGER_KEY,
    queryFn: ({ pageParam }) => api.fetchCreditLedger(50, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  const redeem = useMutation({
    mutationFn: (value: string) => api.redeemCode(value),
    onSuccess: (result) => {
      toast.success(`兑换成功，获得 ${formatCredits(result.credits_added)} 积分`);
      setCode("");
      setError(null);
      qc.invalidateQueries({ queryKey: ["credits"] });
    },
    onError: (err) => {
      const apiErr = err as ApiError;
      setError(redeemErrorMessage(apiErr.code, apiErr.message));
    },
  });

  const submit = () => {
    const normalized = normalizeRedeemCodeInput(code);
    if (normalized.replace(/-/g, "").length !== 16) {
      setError("兑换码应为 16 位字符");
      return;
    }
    redeem.mutate(normalized);
  };

  const entries = ledgerQ.data?.pages.flatMap((p) => p.entries) ?? [];

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Coins className="h-5 w-5" /> 我的积分
          </CardTitle>
          <CardDescription>对话与专家模式按实际模型消耗扣除积分。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-end gap-x-8 gap-y-3">
            <div>
              <div className="text-xs text-muted-foreground">当前余额</div>
              <div className="text-3xl font-semibold tabular-nums">
                {isLoading ? "—" : formatCredits(credits?.balance)}
              </div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">累计获得</div>
              <div className="text-lg tabular-nums">
                {formatCredits(credits?.lifetime_granted)}
              </div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">累计消耗</div>
              <div className="text-lg tabular-nums">
                {formatCredits(credits?.lifetime_consumed)}
              </div>
            </div>
          </div>

          {credits && !credits.enforced ? (
            <div className="flex items-start gap-2 rounded-md border border-border bg-secondary/40 p-3 text-xs text-muted-foreground">
              <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                当前为观察模式：积分会照常扣减与记账，但余额不足时不会中断对话。
              </span>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>兑换积分</CardTitle>
          <CardDescription>输入兑换码，积分立即到账。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-col gap-2 sm:flex-row">
            <Input
              value={code}
              onChange={(e) => {
                setCode(normalizeRedeemCodeInput(e.target.value));
                setError(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") submit();
              }}
              placeholder="XXXX-XXXX-XXXX-XXXX"
              aria-label="兑换码"
              className="font-mono tracking-wider"
              autoComplete="off"
              spellCheck={false}
            />
            <Button
              onClick={submit}
              disabled={redeem.isPending || !code}
              className="sm:w-28"
            >
              {redeem.isPending ? "兑换中…" : "兑换"}
            </Button>
          </div>
          {error ? <p className="text-sm text-destructive">{error}</p> : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>消费明细</CardTitle>
          <CardDescription>最近 50 条，按时间倒序。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {ledgerQ.isLoading ? (
            <p className="text-sm text-muted-foreground">加载中…</p>
          ) : entries.length === 0 ? (
            <p className="text-sm text-muted-foreground">暂无记录。</p>
          ) : (
            <>
              <div className="overflow-x-auto rounded-lg border border-border">
                <table className="w-full text-sm">
                  <thead className="bg-secondary/50 text-left text-xs text-muted-foreground">
                    <tr>
                      <th className="p-3">时间</th>
                      <th className="p-3">原因</th>
                      <th className="p-3 text-right">变动</th>
                      <th className="p-3 text-right">余额</th>
                    </tr>
                  </thead>
                  <tbody>
                    {entries.map((entry) => (
                      <tr key={entry.id} className="border-t border-border">
                        <td className="whitespace-nowrap p-3 text-muted-foreground">
                          {new Date(entry.created_at).toLocaleString()}
                        </td>
                        <td className="p-3">
                          {REASON_LABELS[entry.reason] ?? entry.reason}
                          {entry.note ? (
                            <span className="ml-2 text-xs text-muted-foreground">
                              {entry.note}
                            </span>
                          ) : null}
                        </td>
                        <td
                          className={
                            entry.delta >= 0
                              ? "p-3 text-right tabular-nums text-emerald-600"
                              : "p-3 text-right tabular-nums text-muted-foreground"
                          }
                        >
                          {entry.delta >= 0 ? `+${entry.delta}` : entry.delta}
                        </td>
                        <td className="p-3 text-right tabular-nums">
                          {entry.balance_after}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {ledgerQ.hasNextPage ? (
                <div className="flex justify-center">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={ledgerQ.isFetchingNextPage}
                    onClick={() => ledgerQ.fetchNextPage()}
                  >
                    {ledgerQ.isFetchingNextPage ? "加载中…" : "加载更多"}
                  </Button>
                </div>
              ) : null}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
```

- [ ] **Step 3: 侧边栏常驻余额**

在 `frontend/src/components/sidebar.tsx` 中，找到渲染 `/settings` 链接的那段（约 `:415`），在其**之前**插入余额入口：

```tsx
          <Link
            href={withReturnTo("/settings/credits", returnTo)}
            className="flex items-center justify-between rounded-md px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <span className="flex items-center gap-2">
              <Coins className="h-4 w-4" />
              积分
            </span>
            <span className="font-mono tabular-nums">
              {credits ? formatCredits(credits.balance) : "—"}
            </span>
          </Link>
```

在组件函数体顶部（`returnTo` 之类解构附近）加：

```tsx
  const { credits } = useCredits();
```

并在 import 区加：

```tsx
import { useCredits } from "@/hooks/useCredits";
import { formatCredits } from "@/lib/credits";
```

`Coins` 加到已有的 `lucide-react` import 列表中。

- [ ] **Step 4: 类型检查、测试、构建**

```bash
cd frontend && npx tsc --noEmit && npx vitest run && npm run build
```

Expected: 无类型错误；全部测试通过；构建成功

注意：若此时 `npm run dev` 正在运行，先停掉再 `npm run build`，否则 `.next` 会被写坏，出现 "Cannot find module ./NNN.js" 这类假故障。

- [ ] **Step 5: 手工验证**

启动后端与前端，用普通用户登录：
1. 侧边栏底部显示"积分"与余额
2. 进入 `/settings/credits`，余额卡片显示 0，观察模式提示条可见
3. 输入一个后台生成的兑换码 → toast 成功、余额增加、流水新增一行
4. 同一个码再兑一次 → 显示"该兑换码已被使用"
5. 手输小写、去掉横线的同一码 → 仍然拒绝（已被用过）

- [ ] **Step 6: 提交**

```bash
git add frontend/src/app/settings/credits/page.tsx frontend/src/app/settings/layout.tsx frontend/src/components/sidebar.tsx
git commit -m "feat(credits): 用户侧积分页与侧边栏常驻余额

/settings/credits：余额、累计获得/消耗、兑换表单、消费流水（游标分页）。
侧边栏底部常驻余额入口 —— 预付费模型下用户必须随时看得到还剩多少，
否则被拦截时很突然。

观察模式时页面顶部显示提示条：此时扣分但不拦截，不说清楚用户会困惑。
兑换码输入框实时规范化（大写、补分隔符、修正 I/L/O）。"
```

---

### Task 10: 管理后台 —— 兑换码

**Files:**
- Modify: `frontend/src/app/admin/page.tsx`（新增「兑换码」Tab）

**Interfaces:**
- Consumes: Task 8 的 `api.adminListRedeemBatches`、`api.adminCreateRedeemBatch`、`api.adminListRedeemCodes`、`api.adminVoidRedeemBatch`
- Produces: 管理后台「兑换码」Tab

- [ ] **Step 1: 加 Tab 与内容组件**

在 `frontend/src/app/admin/page.tsx` 中：

1. `TabsList` 内、`审计日志` 之前加：

```tsx
        <TabsTrigger value="redeem">兑换码</TabsTrigger>
```

2. 在 `</Tabs>` 之前加：

```tsx
      {/* Redeem codes — 批次生成、导出、作废 */}
      <TabsContent value="redeem" className="space-y-3">
        <RedeemCodesPanel />
      </TabsContent>
```

3. 在文件末尾追加组件：

```tsx
interface RedeemBatchRow {
  batch: {
    id: string;
    name: string;
    credits_per_code: number;
    expires_at: string | null;
    created_at: string;
  };
  total: number;
  redeemed: number;
  void: number;
  active: number;
}

/**
 * 兑换码面板。
 *
 * 明文码只在创建响应里出现一次 —— 库中只存 SHA-256 哈希，之后再也取不回。
 * 所以创建成功后必须立刻弹出明文供复制 / 下载，并在关掉弹窗后明确提示
 * "明文不会再次显示"。
 */
function RedeemCodesPanel() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [credits, setCredits] = useState("1000");
  const [count, setCount] = useState("10");
  const [expiresAt, setExpiresAt] = useState("");
  const [note, setNote] = useState("");
  const [issued, setIssued] = useState<string[] | null>(null);

  const batchesQ = useQuery({
    queryKey: ["admin-redeem-batches"],
    queryFn: api.adminListRedeemBatches,
  });

  const createMut = useMutation({
    mutationFn: () =>
      api.adminCreateRedeemBatch({
        name: name.trim(),
        credits_per_code: Number(credits),
        count: Number(count),
        expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
        note: note.trim() || null,
      }),
    onSuccess: (result) => {
      setIssued(result.codes);
      setOpen(false);
      qc.invalidateQueries({ queryKey: ["admin-redeem-batches"] });
      toast.success(`已生成 ${result.codes.length} 个兑换码`);
    },
    onError: (err) => {
      const apiErr = err as ApiError;
      toast.error(redeemErrorMessage(apiErr.code, apiErr.message));
    },
  });

  const voidMut = useMutation({
    mutationFn: (batchId: string) => api.adminVoidRedeemBatch(batchId),
    onSuccess: (result) => {
      toast.success(`已作废 ${result.voided} 个未使用的兑换码`);
      qc.invalidateQueries({ queryKey: ["admin-redeem-batches"] });
    },
    onError: () => toast.error("作废失败"),
  });

  const downloadCsv = (codes: string[]) => {
    const rows = ["兑换码", ...codes].join("\n");
    // 加 BOM，否则 Excel 打开中文表头会乱码。
    const blob = new Blob([`﻿${rows}`], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `兑换码-${Date.now()}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <>
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          生成兑换码发给用户，用户在「设置 → 积分」兑换。
        </p>
        <Button size="sm" onClick={() => setOpen(true)}>
          生成兑换码
        </Button>
      </div>

      {batchesQ.isError ? (
        <ErrorState onRetry={() => batchesQ.refetch()} />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="bg-secondary/50 text-left text-xs text-muted-foreground">
              <tr>
                <th className="p-3">批次</th>
                <th className="p-3">面额</th>
                <th className="p-3">核销</th>
                <th className="hidden p-3 sm:table-cell">有效期</th>
                <th className="p-3" />
              </tr>
            </thead>
            <tbody>
              {batchesQ.isLoading ? (
                <tr>
                  <td colSpan={5} className="p-6 text-center text-muted-foreground">
                    加载中…
                  </td>
                </tr>
              ) : !batchesQ.data?.length ? (
                <tr>
                  <td colSpan={5} className="p-6 text-center text-muted-foreground">
                    还没有兑换码批次。
                  </td>
                </tr>
              ) : (
                batchesQ.data.map((row: RedeemBatchRow) => (
                  <tr key={row.batch.id} className="border-t border-border">
                    <td className="p-3 font-medium">{row.batch.name}</td>
                    <td className="p-3 tabular-nums">{row.batch.credits_per_code}</td>
                    <td className="p-3 tabular-nums">
                      {row.redeemed}/{row.total}
                      {row.active > 0 ? (
                        <span className="ml-2 text-xs text-muted-foreground">
                          剩 {row.active}
                        </span>
                      ) : null}
                      {row.void > 0 ? (
                        <span className="ml-2 text-xs text-muted-foreground">
                          作废 {row.void}
                        </span>
                      ) : null}
                    </td>
                    <td className="hidden p-3 text-muted-foreground sm:table-cell">
                      {row.batch.expires_at
                        ? new Date(row.batch.expires_at).toLocaleDateString()
                        : "永久"}
                    </td>
                    <td className="p-3 text-right">
                      {row.active > 0 ? (
                        <Button
                          variant="ghost"
                          size="sm"
                          className="text-destructive"
                          disabled={voidMut.isPending}
                          onClick={() => {
                            if (
                              confirm(
                                `确定作废「${row.batch.name}」剩余的 ${row.active} 个兑换码？已兑换的不受影响。`
                              )
                            ) {
                              voidMut.mutate(row.batch.id);
                            }
                          }}
                        >
                          作废剩余
                        </Button>
                      ) : null}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* 新建批次 */}
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>生成兑换码</DialogTitle>
            <DialogDescription>生成后明文只显示一次，请当场导出。</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1">
              <Label htmlFor="batch-name">批次名称</Label>
              <Input
                id="batch-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="2026 中秋活动"
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <Label htmlFor="batch-credits">每码积分</Label>
                <Input
                  id="batch-credits"
                  type="number"
                  min={1}
                  value={credits}
                  onChange={(e) => setCredits(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="batch-count">生成数量</Label>
                <Input
                  id="batch-count"
                  type="number"
                  min={1}
                  max={5000}
                  value={count}
                  onChange={(e) => setCount(e.target.value)}
                />
              </div>
            </div>
            <div className="space-y-1">
              <Label htmlFor="batch-expires">有效期（留空为永久）</Label>
              <Input
                id="batch-expires"
                type="date"
                value={expiresAt}
                onChange={(e) => setExpiresAt(e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="batch-note">备注</Label>
              <Input
                id="batch-note"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="选填"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>
              取消
            </Button>
            <Button
              disabled={!name.trim() || createMut.isPending}
              onClick={() => createMut.mutate()}
            >
              {createMut.isPending ? "生成中…" : "生成"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 明文码：唯一一次可见 */}
      <Dialog open={!!issued} onOpenChange={(next) => !next && setIssued(null)}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>兑换码已生成</DialogTitle>
            <DialogDescription className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-destructive">
              系统只保存兑换码的哈希值，
              <strong>关闭本窗口后将无法再次查看明文</strong>
              ，请立即复制或下载 CSV。
            </DialogDescription>
          </DialogHeader>
          <pre className="max-h-64 overflow-auto rounded-md border border-border bg-secondary/40 p-3 font-mono text-xs">
            {(issued ?? []).join("\n")}
          </pre>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                navigator.clipboard.writeText((issued ?? []).join("\n"));
                toast.success("已复制");
              }}
            >
              复制全部
            </Button>
            <Button variant="outline" onClick={() => downloadCsv(issued ?? [])}>
              下载 CSV
            </Button>
            <Button onClick={() => setIssued(null)}>我已保存</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
```

4. 在文件顶部补 import（`Dialog` 系列与 `Label`）：

```tsx
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { ApiError } from "@/lib/api";
import { redeemErrorMessage } from "@/lib/credits";
```

注意：文件当前的 import 里已有 `useMutation, useQuery, useQueryClient`、`toast`、`Button`、`Badge`、`Switch`、`Tabs*`。`useState` 若未导入必须补上（本组件用到）。`Input` 与 `Label` 是既有 UI 组件（`src/components/ui/`），不要自己手写 `<input>` / `<label>`。

- [ ] **Step 2: 类型检查与构建**

```bash
cd frontend && npx tsc --noEmit && npx vitest run && npm run build
```

Expected: 无类型错误；测试通过；构建成功

- [ ] **Step 3: 手工验证**

以管理员登录 `/admin` → 「兑换码」：
1. 点「生成兑换码」，填名称 / 面额 1000 / 数量 3 → 生成
2. 弹窗显示 3 个明文码，复制与下载 CSV 均可用
3. 关掉弹窗后，列表显示 3/0（剩余 3），且页面上再也找不到明文
4. 用普通用户兑掉其中一个 → 刷新后台，显示 1/3（剩 2）
5. 点「作废剩余」→ 确认后显示 作废 2，剩 0
6. 用剩余任一码去兑换 → "该兑换码已作废"

- [ ] **Step 4: 提交**

```bash
git add frontend/src/app/admin/page.tsx
git commit -m "feat(credits): 管理后台兑换码面板

批次生成（面额/数量/有效期/备注）、核销进度、整批作废。
生成后弹窗展示明文码，带复制与 CSV 下载（含 BOM，避免 Excel 中文乱码）。

弹窗内明确警示"关闭后无法再次查看明文"—— 库里只存哈希。"
```

---

### Task 11: 管理后台 —— 用户积分

**Files:**
- Modify: `frontend/src/app/admin/page.tsx`（新增「积分」Tab）

**Interfaces:**
- Consumes: Task 8 的 `api.adminListCreditAccounts`、`api.adminAdjustCredits`
- Produces: 管理后台「积分」Tab（余额列表 + 调分）

- [ ] **Step 1: 加 Tab 与内容组件**

在 `frontend/src/app/admin/page.tsx` 中：

1. `TabsList` 内、`审计日志` 之前加：

```tsx
        <TabsTrigger value="credits">积分</TabsTrigger>
```

2. 在 `</Tabs>` 之前加：

```tsx
      {/* Credits — 用户余额与手动调分 */}
      <TabsContent value="credits" className="space-y-3">
        <CreditsPanel />
      </TabsContent>
```

3. 在文件末尾追加组件：

```tsx
interface CreditRow {
  user_id: string;
  email: string;
  username: string;
  balance: number;
  lifetime_granted: number;
  lifetime_consumed: number;
}

/**
 * 用户积分面板。
 *
 * 手动调分是客服必备：支付成功但码没发出去、需要赔偿用户、测试账号充值 ——
 * 这些都不该逼管理员去生成一个一次性兑换码。调分与兑换码走同一套账本，
 * 并写一条 `credits:adjust` 审计事件。
 */
function CreditsPanel() {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [target, setTarget] = useState<CreditRow | null>(null);
  const [delta, setDelta] = useState("");
  const [note, setNote] = useState("");

  const accountsQ = useQuery({
    queryKey: ["admin-credit-accounts", search],
    queryFn: () => api.adminListCreditAccounts(search.trim() || undefined),
  });

  const adjustMut = useMutation({
    mutationFn: (row: CreditRow) =>
      api.adminAdjustCredits({
        user_id: row.user_id,
        delta: Number(delta),
        note: note.trim() || null,
      }),
    onSuccess: (updated) => {
      toast.success(`${updated.username} 当前余额 ${updated.balance}`);
      setTarget(null);
      setDelta("");
      setNote("");
      qc.invalidateQueries({ queryKey: ["admin-credit-accounts"] });
    },
    onError: (err) => {
      const apiErr = err as ApiError;
      toast.error(apiErr.message || "调分失败");
    },
  });

  return (
    <>
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          查看用户余额，或手动加 / 扣积分（会留审计记录）。
        </p>
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="搜索邮箱或用户名"
          className="max-w-56"
        />
      </div>

      {accountsQ.isError ? (
        <ErrorState onRetry={() => accountsQ.refetch()} />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="bg-secondary/50 text-left text-xs text-muted-foreground">
              <tr>
                <th className="p-3">用户</th>
                <th className="hidden p-3 sm:table-cell">邮箱</th>
                <th className="p-3 text-right">余额</th>
                <th className="hidden p-3 text-right md:table-cell">累计获得</th>
                <th className="hidden p-3 text-right md:table-cell">累计消耗</th>
                <th className="p-3" />
              </tr>
            </thead>
            <tbody>
              {accountsQ.isLoading ? (
                <tr>
                  <td colSpan={6} className="p-6 text-center text-muted-foreground">
                    加载中…
                  </td>
                </tr>
              ) : !accountsQ.data?.length ? (
                <tr>
                  <td colSpan={6} className="p-6 text-center text-muted-foreground">
                    没有匹配的用户。
                  </td>
                </tr>
              ) : (
                accountsQ.data.map((row: CreditRow) => (
                  <tr key={row.user_id} className="border-t border-border">
                    <td className="p-3 font-medium">{row.username}</td>
                    <td className="hidden p-3 text-muted-foreground sm:table-cell">
                      {row.email}
                    </td>
                    <td className="p-3 text-right tabular-nums">
                      {formatCredits(row.balance)}
                    </td>
                    <td className="hidden p-3 text-right tabular-nums text-muted-foreground md:table-cell">
                      {row.lifetime_granted}
                    </td>
                    <td className="hidden p-3 text-right tabular-nums text-muted-foreground md:table-cell">
                      {row.lifetime_consumed}
                    </td>
                    <td className="p-3 text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setTarget(row);
                          setDelta("");
                          setNote("");
                        }}
                      >
                        调整
                      </Button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      <Dialog open={!!target} onOpenChange={(next) => !next && setTarget(null)}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>调整积分</DialogTitle>
            <DialogDescription>
              {target?.username}（当前 {formatCredits(target?.balance)}）
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1">
              <Label htmlFor="adjust-delta">变动数量（正数增加，负数扣减）</Label>
              <Input
                id="adjust-delta"
                type="number"
                value={delta}
                onChange={(e) => setDelta(e.target.value)}
                placeholder="例如 1000 或 -500"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="adjust-note">备注</Label>
              <Input
                id="adjust-note"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="例如 客服补偿"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setTarget(null)}>
              取消
            </Button>
            <Button
              disabled={!delta || Number(delta) === 0 || adjustMut.isPending}
              onClick={() => target && adjustMut.mutate(target)}
            >
              {adjustMut.isPending ? "提交中…" : "确认"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
```

4. import 区补充：

```tsx
import { formatCredits } from "@/lib/credits";
import { Label } from "@/components/ui/label";
```

（`Dialog` 系列、`Input`、`ApiError`、`useState` 已在 Task 10 导入）

- [ ] **Step 2: 类型检查与构建**

```bash
cd frontend && npx tsc --noEmit && npx vitest run && npm run build
```

Expected: 无类型错误；测试通过；构建成功

- [ ] **Step 3: 手工验证**

以管理员登录 `/admin` → 「积分」：
1. 列表显示所有用户与余额，种子用户余额是前面手动验证留下的值
2. 搜索邮箱片段能过滤
3. 点「调整」，输入 `1000` + 备注 `客服补偿` → 余额 +1000
4. 再调 `-300` → 余额 -300，累计消耗增加 300
5. 输入 `0` 时确认按钮禁用
6. 到「审计日志」Tab 能看到 `credits:adjust` 事件

- [ ] **Step 4: 提交**

```bash
git add frontend/src/app/admin/page.tsx
git commit -m "feat(credits): 管理后台用户积分面板

余额列表（含搜索）、手动加/扣积分。调分是客服必备：支付成功但码没发
出去、需要赔偿用户、测试账号充值，都不该逼管理员去生成一次性兑换码。

调分与兑换码走同一套账本，并写 credits:adjust 审计事件。"
```

---

### Task 12: 运维文档与整体验证

**Files:**
- Create: `docs/credits-operations.md`
- Modify: `README.md`（如存在环境变量章节则补一行指向运维文档）

**Interfaces:**
- Consumes: 前 11 个 Task 的全部产出
- Produces: 上线操作手册

- [ ] **Step 1: 写运维文档**

创建 `docs/credits-operations.md`：

```markdown
# 积分与兑换码 — 运维手册

## 上线顺序

**不要跳过第 3 步。** `CREDITS_ENFORCED` 默认 `false`（观察模式），这是有意
设计的：直接开拦截会把所有余额为 0 的老用户在部署瞬间锁在外面。

1. **部署迁移**（建表 + 为存量用户回填账户行）。此时功能对用户不可见。
   ```bash
   # compose 部署
   docker compose -f docker-compose.prod.yml run --rm migrate
   # 或裸机部署
   bash deploy/mychat-deploy.sh
   ```
2. **保持 `CREDITS_ENFORCED=false`**，在后台「兑换码」Tab 生成一批码并发放。
3. **核对三项**：
   - 用户兑换后余额增量符合预期
   - 真实对话的扣分数字合理（在「设置 → 积分」的流水里看「对话消耗」）
   - **对账 SQL 无输出**（见下）
4. **打开拦截**：把 `CREDITS_ENFORCED=true` 写入 `.env` 并重启后端。

## 对账 SQL

余额是派生值（真相是账本流水求和），两者可能因程序缺陷漂移。这条查询
返回**任何行**都表示需要人工介入：

```sql
SELECT a.user_id, a.balance, COALESCE(SUM(l.delta), 0) AS ledger_sum
  FROM credit_accounts a
  LEFT JOIN credit_ledger l ON l.user_id = a.user_id
 GROUP BY a.user_id, a.balance
HAVING a.balance <> COALESCE(SUM(l.delta), 0);
```

修复方式：不要直接改 `credit_accounts.balance`（那会绕过账本，下次对账仍然
报警）。应该用后台的「调整积分」插一条 `admin_adjust` 流水把差额补上，并在
备注里写明原因。

建议把这条查询挂进日常巡检（例如随备份脚本一起跑）。

## 并发演练（首次上线前做一次）

测试套件跑在内存 SQLite 上，而 SQLite 是单写者模型 —— **它无法验证真并发**。
积分涉及钱，所以真多写者的行为必须在真实 Postgres 上手动验一次。

### 演练一：同一个码被并发兑换

开两个 psql 会话，都先读后写，验证只有一个能拿到行：

```sql
-- 会话 A 与 B 同时执行；把 :code_hash 换成某个 active 码的哈希
BEGIN;
UPDATE redeem_codes
   SET status = 'redeemed', redeemed_by = '<uuid>', redeemed_at = now()
 WHERE code_hash = :code_hash AND status = 'active';
-- 期望：其中一个会话 rowcount = 1，另一个 rowcount = 0
```

预期：**恰好一个 1，一个 0**。若两个都是 1，说明 CAS 的 WHERE 没生效，立刻停下排查。

### 演练二：并发发放不丢钱

```sql
-- 两个会话同时对同一用户插一条发放流水并更新余额
BEGIN;
SELECT balance FROM credit_accounts WHERE user_id = :uid FOR UPDATE;   -- 应阻塞其中一个
UPDATE credit_accounts SET balance = balance + 100 WHERE user_id = :uid;
COMMIT;
```

预期：`SELECT ... FOR UPDATE` 会阻塞第二个会话直到第一个提交；两个都提交后余额
恰好 +200。若第二个会话没被阻塞，说明行锁没生效。

### 演练三：账本幂等

```sql
-- 故意插一条重复的扣费流水
INSERT INTO credit_ledger (id, user_id, delta, balance_after, reason, ref_type, ref_id, created_at)
VALUES (gen_random_uuid(), :uid, -10, 0, 'usage', 'message', :msg_id, now());
-- 再插一次完全相同的 (ref_type, ref_id, reason)
```

预期：第二次抛 `duplicate key value violates unique constraint "uq_credit_ledger_ref"`。
若第二次成功，说明唯一部分索引没建上，检查迁移 0014 是否完整执行。

## 常见问题

### 用户说兑换码用不了

先问清楚错误提示：

| 提示 | 含义 | 处理 |
|---|---|---|
| 兑换码不存在 | 输错了，或这个码不属于本平台 | 让用户核对，注意 `0`/`O`、`1`/`I` 已自动纠正 |
| 该兑换码已被使用 | 已经被兑过 | 后台「兑换码」Tab 查该批次的码列表，用前缀定位是不是同一张 |
| 该兑换码已过期 | 超过批次有效期 | 无法恢复，需要重新发一张 |
| 该兑换码已作废 | 批次被整批作废 | 同上 |

### 兑换码明文丢了

**无法找回。** 库里只存 SHA-256 哈希，这是有意设计（兑换码是不记名凭证，
等价于现金；明文入库意味着一次库泄露就等于漏钱）。处理方式：把该批作废，
重新生成一批。已兑换出去的分数不受影响。

### 用户余额是负数

正常现象，不是 bug。准入检查在对话开始前、扣费在对话结束后，所以最后一轮
会把余额扣成负数 —— 平台确实为那一轮付了钱。用户侧显示为 0，后台显示真实
负值。下一位用户兑换后会自然补正。

### 想给某个用户补分

后台「积分」Tab → 搜索该用户 → 「调整」→ 填正数 + 备注。会写一条
`credits:adjust` 审计事件，「审计日志」Tab 可查。

## 配置项

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `CREDITS_ENFORCED` | `false` | 观察模式开关。`false` = 扣分记账但不拦截 |
| `CREDITS_PER_USD` | `1000` | 1 美元服务端实测成本折算多少积分 |
| `CREDITS_PER_1K_TOKENS_FALLBACK` | `1` | 未配置定价的模型每千 token 兜底扣分 |
| `CREDITS_SIGNUP_BONUS` | `0` | 注册赠送积分，0 = 不送 |
| `CREDITS_MAX_ADJUST` | `10000000` | 单次管理员调分上限 |
| `REDEEM_MAX_CODES_PER_BATCH` | `5000` | 单批生成上限 |

### 定价与积分的关系

扣分优先按 `MODEL_PRICING_JSON` 算出的服务端实测成本换算：

```
积分 = ceil(cost_usd × CREDITS_PER_USD)
```

**未在该表里配置的模型**（`usage_cost()` 返回 `None`）会回落到 token 计价：

```
积分 = ceil(total_tokens / 1000 × CREDITS_PER_1K_TOKENS_FALLBACK)
```

所以新增模型时记得同时配置 `MODEL_PRICING_JSON`，否则它会按兜底费率计费 ——
兜底费率与真实成本可能差很多，贵模型会被低估。

## 与配额（QUOTAS）的关系

两者独立叠加，互不影响：

- **配额**（`QUOTAS_ENABLED`）是管理员配的**上限**，不随充值增长，用户看不到。
- **积分**（`CREDITS_ENFORCED`）是用户可兑换的**余额**，有流水，用户可见。

任意一个开着都会拦截超限的请求，错误码不同（`quota_exceeded` vs
`insufficient_credits`）。
```

- [ ] **Step 2: 在 README 里加指引**

在 `README.md` 的环境变量或功能章节附近加一句：

```markdown
积分与兑换码的运维操作（上线顺序、对账 SQL、常见问题）见
[docs/credits-operations.md](docs/credits-operations.md)。
```

- [ ] **Step 3: 全量后端测试**

```bash
cd backend && python -m pytest tests/ -q
```

Expected: 通过。**已知的环境相关失败可以接受**（不是本次改动引入）：
- `test_multi_agent_approval_pauses_then_resumes` —— 既有死锁，会连带 ~50 个 `OperationalError`
- `test_agent_phase2` 的 `plan_created` / `test_agent_phase5` 的 `full_native` —— 模型端点不可达时 502

若出现其他失败，必须定位到根因再继续。

- [ ] **Step 4: 全量前端检查**

```bash
cd frontend && npx tsc --noEmit && npx vitest run && npm run build
```

Expected: 全部通过

- [ ] **Step 5: 端到端手工验收**

1. 管理员生成一批 3 张 10000 分的码
2. 普通用户兑换一张 → 余额 10000，流出流水 `+10000 兑换码`
3. 该用户发一条对话 → 流水出现 `-N 对话消耗`，余额下降，且 N 与模型成本相称
4. 同一张码再兑 → 「该兑换码已被使用」
5. 把余额调到 0（管理员调分）→ 打开 `CREDITS_ENFORCED=true` 重启 → 用户发对话 → 「积分不足」
6. 兑换第二张码 → 用户发对话 → 正常
7. 跑对账 SQL → 无输出

- [ ] **Step 6: 提交**

```bash
git add docs/credits-operations.md README.md
git commit -m "docs(credits): 运维手册

上线顺序（强调必须先观察模式再开拦截）、对账 SQL、常见问题排查表、
定价与积分的关系、与既有配额的边界。

对账 SQL 建议随备份脚本一起跑；修复漂移要走调分流水而不是直接改余额。"
```

---

## 完成标准

全部 12 个 Task 完成后，以下断言必须成立：

1. `cd backend && python -m pytest tests/ -q` 通过（除已知环境相关失败）
2. `cd frontend && npx tsc --noEmit && npx vitest run && npm run build` 通过
3. 对账 SQL 返回 0 行
4. `git log --oneline feat/credits-redeem-codes` 显示 12 个功能提交
5. 端到端手工验收（Task 12 Step 5）七步全过
