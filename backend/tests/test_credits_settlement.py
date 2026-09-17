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
    await settle_turn_usage(db_session, uid, msg, "gpt-4o", {"prompt_tokens": 100, "completion_tokens": 100, "cost_usd": 0.02})
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
