"""运行控制下沉：暂停 / 恢复 / 取消 / 追加指令 + 持久命令 drain。

引擎路径之前完全不消费 run_control —— 用户按暂停没有任何反应。这两个方法
搬到 RunEnvironment 后，两条 walker 共享同一套控制语义。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.run_controls import get_or_create


async def _env(db_session):
    from tests.test_run_environment import _env_with_graph

    return await _env_with_graph(db_session)


async def test_cancel_raises_cancelled_error(db_session):
    env = await _env(db_session)
    ctl = get_or_create(env.run_id)
    ctl.cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await env.respect_controls()


async def test_instructions_are_drained_into_stage_ctx(db_session):
    env = await _env(db_session)
    ctl = get_or_create(env.run_id)
    ctl.add_instruction("聚焦 X")

    await env.respect_controls()

    assert env.stage_ctx.pending_instructions == ["聚焦 X"]
    assert ctl.drain_instructions() == [], "指令必须被一次性取走"


async def test_instruction_emits_event(db_session):
    env = await _env(db_session)
    await asyncio.sleep(0)
    while not env.stage_ctx.queue.empty():
        env.stage_ctx.queue.get_nowait()
    ctl = get_or_create(env.run_id)
    ctl.add_instruction("跳过 Y")

    await env.respect_controls()
    await asyncio.sleep(0)

    kinds = []
    while not env.stage_ctx.queue.empty():
        evt = env.stage_ctx.queue.get_nowait()
        if evt is not None:
            kinds.append(evt.kind)
    assert "run_instruction_received" in kinds


async def test_pause_blocks_until_resumed(db_session):
    env = await _env(db_session)
    ctl = get_or_create(env.run_id)
    ctl.pause()

    task = asyncio.create_task(env.respect_controls())
    await asyncio.sleep(0.05)
    assert not task.done(), "暂停中 respect_controls 不应返回"

    ctl.resume()
    await asyncio.wait_for(task, timeout=1.0)


async def test_pause_then_cancel_unblocks(db_session):
    env = await _env(db_session)
    ctl = get_or_create(env.run_id)
    ctl.pause()

    task = asyncio.create_task(env.respect_controls())
    await asyncio.sleep(0.05)
    ctl.cancel.set()
    await asyncio.wait_for(task, timeout=1.0)


async def test_no_run_control_is_a_noop(db_session):
    env = await _env(db_session)
    env.ctx.extra.pop("run_control", None)
    get_or_create(env.run_id)  # 注册空控制，不应阻塞
    await asyncio.wait_for(env.respect_controls(), timeout=1.0)
