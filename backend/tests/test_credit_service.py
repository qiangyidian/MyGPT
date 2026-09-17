"""积分服务测试：记账、扣分、幂等、透支、调分、分页。"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.models import CreditAccount, CreditLedger, Message
from app.services import credit_service

SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest_asyncio.fixture(autouse=True)
async def _clean_credit_tables(db_session):
    """Clear residual credit rows before each test.

    credit_service 只 flush 不 commit（事务边界归调用方），而测试引擎用
    StaticPool 单连接共享内存库；``begin_nested()`` 内的行在 SQLite 上会随
    savepoint 释放而残留到连接级。显式清一次，保证每个用例从空状态开始。
    """
    await db_session.execute(sa.delete(CreditLedger))
    await db_session.execute(sa.delete(CreditAccount))
    await db_session.commit()
    yield


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
