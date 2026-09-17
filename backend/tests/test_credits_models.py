"""积分四张表的约束测试。重点验证"不允许出现第二次"的数据库级保证。"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import CreditAccount, CreditLedger, RedeemCode, RedeemCodeBatch, User


async def _seed_user(db_session) -> uuid.UUID:
    """建一个真实用户并返回 id。

    涉及 users 外键的用例都要用它而不是 ``uuid.uuid4()`` —— 理由见
    :func:`test_ledger_ref_triple_is_unique`。
    """
    token = uuid.uuid4().hex[:12]
    user = User(
        email=f"{token}@example.com",
        username=f"u{token}",
        password_hash="x",
        role="user",
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    return user.id


async def test_ledger_ref_triple_is_unique(db_session):
    """同一 (ref_type, ref_id, reason) 只能有一行 —— 这是扣费幂等的根基。

    用**真实存在的用户**，不图省事用 ``uuid.uuid4()``：SQLite 默认不启用
    外键约束，但万一哪天开了，伪造的 user_id 会先撞外键 —— 而
    ``pytest.raises(IntegrityError)`` 会把外键违规也当成通过，测试就为错误的
    原因变绿了。真实用户让唯一索引成为唯一可能的违规来源。
    """
    uid = await _seed_user(db_session)
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
    uid = await _seed_user(db_session)
    db_session.add_all(
        [
            CreditLedger(user_id=uid, delta=10, balance_after=10, reason="admin_adjust"),
            CreditLedger(user_id=uid, delta=10, balance_after=20, reason="admin_adjust"),
        ]
    )
    await db_session.flush()  # 不应抛异常


async def test_ledger_different_reason_same_ref_is_allowed(db_session):
    uid = await _seed_user(db_session)
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
    uid = await _seed_user(db_session)
    db_session.add(CreditAccount(user_id=uid, balance=-42))
    await db_session.flush()


async def test_batch_rejects_non_positive_credits(db_session):
    db_session.add(RedeemCodeBatch(name="bad", credits_per_code=0))
    with pytest.raises(IntegrityError):
        await db_session.flush()
