"""余额拦截测试：观察模式放行、强制模式拦截、两条执行路径都覆盖。

注意两条路径的入口**是同一个路由** `POST /api/chat/stream`：
`BACKGROUND_WORKER="inprocess"`（测试默认，见 conftest）走内联执行，
其余值走 durable 分发（见 `app/api/chat.py:197`）。所以用 monkeypatch 切。
"""
from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.core.config import get_settings
from app.credits import CreditPolicy, get_credit_policy, set_credit_policy
from app.models import CreditAccount, CreditLedger
from app.services import credit_service
from tests.conftest import auth_headers

SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


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


async def _drain(db, user_id):
    """把这个用户的积分账户清掉，让它回到"余额为 0 / 无账户行"的状态。

    为什么必须有这个：测试库是 session 级共享的（conftest 的 seeded_db），而
    `test_durable_path_passes_when_funded` 会给**种子用户**加 10000 分并且提交。
    下面那些"余额 0 应当被拦截"的用例如果依赖种子用户天然是 0，就只是在靠
    **定义顺序**侥幸通过 —— 一旦有人调整用例顺序、插入新用例，或者别的文件
    动了这个用户的余额，它们就会失败，而且失败原因极难定位。

    显式清一次，把前提写进用例本身，而不是依赖执行顺序。
    """
    await db.execute(sa.delete(CreditLedger).where(CreditLedger.user_id == user_id))
    await db.execute(sa.delete(CreditAccount).where(CreditAccount.user_id == user_id))
    await db.commit()


async def test_observation_mode_reports_not_enforced(client, auth_token, observing):
    me = (await client.get("/api/credits/me", headers=auth_headers(auth_token))).json()
    assert me["enforced"] is False


async def test_enforcing_mode_reports_enforced(client, auth_token, enforcing):
    me = (await client.get("/api/credits/me", headers=auth_headers(auth_token))).json()
    assert me["enforced"] is True


# ---- 内联路径：SSE error 事件 ---------------------------------------------- #

async def test_inline_path_blocks_zero_balance_when_enforcing(
    client, auth_token, enforcing, db_session, offline_model
):
    # 显式把种子用户清成"无账户行" —— 不靠定义顺序侥幸（见 _drain 的说明）。
    await _drain(db_session, SEEDED_USER)
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
    client, auth_token, observing, db_session, offline_model
):
    await _drain(db_session, SEEDED_USER)
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
    # 显式清空，不依赖定义顺序（见 _drain 的说明）。
    await _drain(db_session, SEEDED_USER)
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
    # 先清再充，保证这个用例的前提与其他用例无关。
    await _drain(db_session, SEEDED_USER)
    h = auth_headers(auth_token)
    model_id = await _create_mock_model(client, h)
    await _fund(db_session, SEEDED_USER)

    # 测试环境没有 Redis —— 注入内存队列，和 test_durable_dispatch 的做法一致。
    # 流式响应在 run 到终态前不会结束，所以要顺手用 RunWorker 把它跑完。
    import asyncio

    from app.agents.workflow.queue import InMemoryQueue, set_run_queue
    from app.agents.workflow.execution import execute_run
    from app.agents.workflow.worker import RunWorker
    from app.services.chat_service import chat_service
    from tests.conftest import TestSessionLocal

    _queue = InMemoryQueue()
    _orig_factory = chat_service._persistence_session_factory
    chat_service._persistence_session_factory = TestSessionLocal
    set_run_queue(_queue)

    async def _drive_worker():
        # 给 dispatch 一点时间建 run 并入队，然后跑一次 worker 到终态。
        await asyncio.sleep(0.2)
        worker = RunWorker(
            queue=_queue,
            execute_fn=execute_run,
            session_factory=TestSessionLocal,
        )
        await worker.run_once()

    try:
        worker_task = asyncio.create_task(_drive_worker())

        async with client.stream(
            "POST",
            "/api/chat/stream",
            json={"content": "hi", "model_id": model_id},
            headers=h,
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers.get("x-durable-run-id") is not None
            # 消费流直到终态（run 不跑完流不会结束）。
            async for chunk in resp.aiter_text():
                pass

        await worker_task
    finally:
        set_run_queue(None)
        chat_service._persistence_session_factory = _orig_factory


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
