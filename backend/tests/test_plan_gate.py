"""计划门：计划先行，默认不阻塞；用户主动上闸才等待确认。"""
from __future__ import annotations

import asyncio

import pytest

from app.agents.run_controls import get_or_create
from app.core.config import get_settings


def test_plan_gate_defaults():
    s = get_settings()
    assert s.PLAN_REQUIRE_CONFIRMATION is True
    assert s.PLAN_CONFIRM_TIMEOUT_S == 90


def test_run_control_gate_flag_roundtrip():
    ctl = get_or_create("gate-test")
    assert ctl.gate_requested is False
    ctl.request_gate()
    assert ctl.gate_requested is True


async def test_await_confirmation_returns_immediately_when_not_gated(db_session):
    from tests.test_run_environment import _env_with_graph

    env = await _env_with_graph(db_session)
    ctl = get_or_create(env.run_id)

    async def _never_confirmed() -> str | None:
        return "draft"

    # 用户没上闸 → 立即返回 True（不阻塞）
    ok = await asyncio.wait_for(
        env.await_plan_confirmation(_never_confirmed), timeout=0.5
    )
    assert ok is True
    assert ctl.gate_requested is False


async def test_await_confirmation_waits_when_gated(db_session, monkeypatch):
    from tests.test_run_environment import _env_with_graph

    monkeypatch.setattr(
        get_settings(), "PLAN_CONFIRM_TIMEOUT_S", 2, raising=False
    )
    env = await _env_with_graph(db_session)
    ctl = get_or_create(env.run_id)
    ctl.request_gate()

    confirmed = {"v": False}

    async def _status() -> str | None:
        return "confirmed" if confirmed["v"] else "draft"

    task = asyncio.create_task(env.await_plan_confirmation(_status))
    await asyncio.sleep(0.1)
    assert not task.done(), "已上闸时应等待"

    confirmed["v"] = True
    ok = await asyncio.wait_for(task, timeout=3.0)
    assert ok is True


async def test_await_confirmation_times_out_and_proceeds(db_session, monkeypatch):
    from tests.test_run_environment import _env_with_graph

    monkeypatch.setattr(
        get_settings(), "PLAN_CONFIRM_TIMEOUT_S", 1, raising=False
    )
    env = await _env_with_graph(db_session)
    ctl = get_or_create(env.run_id)
    ctl.request_gate()

    async def _status() -> str | None:
        return "draft"

    ok = await asyncio.wait_for(env.await_plan_confirmation(_status), timeout=4.0)
    # 超时后按默认计划继续（用户没响应不应导致任务失败）
    assert ok is False


async def test_await_confirmation_honors_cancel(db_session, monkeypatch):
    from tests.test_run_environment import _env_with_graph

    monkeypatch.setattr(
        get_settings(), "PLAN_CONFIRM_TIMEOUT_S", 30, raising=False
    )
    env = await _env_with_graph(db_session)
    ctl = get_or_create(env.run_id)
    ctl.request_gate()

    async def _status() -> str | None:
        return "draft"

    task = asyncio.create_task(env.await_plan_confirmation(_status))
    await asyncio.sleep(0.1)
    ctl.cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
