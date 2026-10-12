"""sweeper 补做漏掉的回合（spec §4.2.1）。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import TestSessionLocal
from tests.memory.helpers import add_message, commit_events, create_conversation


@pytest.mark.asyncio
async def test_sweeper_commits_unprocessed_turn(db_session, offline_model):
    from app.agents.memory.sweeper import MemorySweeper

    conversation_id = await create_conversation(db_session)
    await add_message(db_session, conversation_id, "user", "我喜欢用 Python 写脚本")
    await add_message(db_session, conversation_id, "assistant", "好的")
    await db_session.commit()

    sweeper = MemorySweeper(TestSessionLocal, leader_name=None, interval_seconds=600)
    # 返回值是全局计数（扫描面不止本测试的会话），所以只用 >= 1；精确断言按本会话。
    assert await sweeper.run_once() >= 1
    assert len(await commit_events(conversation_id)) == 1

    # 再跑一次：本会话已有锚点，不重复补做。
    await sweeper.run_once()
    assert len(await commit_events(conversation_id)) == 1


@pytest.mark.asyncio
async def test_sweeper_skips_out_of_window(db_session, offline_model):
    """窗口外的旧回合不补：补做是兜底，不是全量重算。"""
    from app.agents.memory.sweeper import MemorySweeper

    conversation_id = await create_conversation(db_session)
    old = datetime.now(UTC) - timedelta(hours=48)
    await add_message(db_session, conversation_id, "user", "老消息")
    await add_message(db_session, conversation_id, "assistant", "老的回答", created_at=old)
    await db_session.commit()

    sweeper = MemorySweeper(TestSessionLocal, leader_name=None, interval_seconds=600)
    await sweeper.run_once()
    assert await commit_events(conversation_id) == []


@pytest.mark.asyncio
async def test_sweeper_batch_limit(db_session, offline_model, monkeypatch):
    """单轮有上限：一次事故后不该攒出一大批待补回合把预算打光。"""
    from app.agents.memory.sweeper import MemorySweeper
    from app.core.config import get_settings

    for _ in range(3):
        conversation_id = await create_conversation(db_session)
        await add_message(db_session, conversation_id, "user", "我喜欢用 Python")
        await add_message(db_session, conversation_id, "assistant", "好的")
    await db_session.commit()

    monkeypatch.setattr(get_settings(), "MEMORY_SWEEP_BATCH", 2, raising=False)
    sweeper = MemorySweeper(TestSessionLocal, leader_name=None, interval_seconds=600)
    # 三个待补回合 + 上限 2 → 恰好补 2 个（本测试自己保证待补数 ≥ 上限）。
    assert await sweeper.run_once() == 2
