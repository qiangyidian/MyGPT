"""回合级写入入口：模式闸门、幂等、候选产出（spec §4.2 / §4.3）。"""
from __future__ import annotations

import uuid

import pytest

from app.agents.intent_router import memory_mode
from tests.conftest import TestSessionLocal
from tests.memory.helpers import (
    SEEDED_USER_ID,
    add_message,
    commit_events,
    create_conversation,
    user_memories_for_conversation,
)


@pytest.mark.parametrize(
    "mode,inject_summary,inject_working,inject_facts,inject_episodes,write,model_tool",
    [
        ("speed", True, False, False, False, True, False),
        ("expert", True, True, True, True, True, True),
        ("auto", True, True, True, True, True, True),
        ("hermes", False, False, False, False, False, False),
        ("", False, False, False, False, False, False),   # 未知值按 hermes：不给未知路由写权
    ],
)
def test_mode_matrix(mode, inject_summary, inject_working, inject_facts,
                     inject_episodes, write, model_tool):
    m = memory_mode(mode)
    assert (m.inject_summary, m.inject_working, m.inject_facts,
            m.inject_episodes, m.write, m.model_tool) == (
        inject_summary, inject_working, inject_facts, inject_episodes, write, model_tool
    )


from app.agents.memory.facade import commit_turn


async def _seed_turn(db, *, user_content: str = "我喜欢用 Python 写脚本") -> tuple:
    """建一个会话 + 一轮 user/assistant 消息，返回 (conversation_id, assistant_id)。"""
    conversation_id = await create_conversation(db)
    await add_message(db, conversation_id, "user", user_content)
    assistant_id = await add_message(db, conversation_id, "assistant", "好的")
    await db.commit()
    return conversation_id, assistant_id


@pytest.mark.asyncio
async def test_commit_turn_is_idempotent(db_session, offline_model):
    conversation_id, assistant_id = await _seed_turn(db_session)

    first = await commit_turn(
        conversation_id=conversation_id, user_id=SEEDED_USER_ID,
        assistant_message_id=assistant_id, mode="expert",
        session_factory=TestSessionLocal,
    )
    second = await commit_turn(
        conversation_id=conversation_id, user_id=SEEDED_USER_ID,
        assistant_message_id=assistant_id, mode="expert",
        session_factory=TestSessionLocal,
    )
    assert first.committed is True
    assert second.committed is False
    assert second.skipped_reason == "already_committed"
    # 按本会话限定，不写全局计数（内存库同进程共享）。
    assert len(await commit_events(conversation_id)) == 1


@pytest.mark.asyncio
async def test_commit_turn_records_candidate(db_session, offline_model):
    # 与本文件上一测试用**不同的**用户内容：propose_candidate 按 (user_id, content)
    # 全局去重，而内存库同进程共享 —— 同文案会让上一轮先插入的候选把这一轮挤掉
    # （见 tests/memory/helpers.py 的共享库约定）。
    conversation_id, assistant_id = await _seed_turn(
        db_session, user_content="我喜欢用 Rust 写服务"
    )
    result = await commit_turn(
        conversation_id=conversation_id, user_id=SEEDED_USER_ID,
        assistant_message_id=assistant_id, mode="expert",
        session_factory=TestSessionLocal,
    )
    assert result.committed is True
    assert result.candidates >= 1
    rows = await user_memories_for_conversation(conversation_id)
    assert rows, "这一轮应当产出候选"
    assert all(active is False for _id, _content, active, _source in rows), "S1 的候选一律 inactive"
    assert all(source == "inferred" for _id, _content, _active, source in rows)


@pytest.mark.asyncio
async def test_commit_turn_marks_hermes_without_writing(db_session, offline_model):
    conversation_id, assistant_id = await _seed_turn(db_session)
    result = await commit_turn(
        conversation_id=conversation_id, user_id=SEEDED_USER_ID,
        assistant_message_id=assistant_id, mode="hermes",
        session_factory=TestSessionLocal,
    )
    assert result.committed is True
    assert result.skipped_reason == "mode_disabled"
    # 仍然打标：不读不写也要记"这轮处理过了"，否则 sweeper 会一直重试。
    assert await commit_events(conversation_id)
    assert await user_memories_for_conversation(conversation_id) == []


@pytest.mark.asyncio
async def test_missing_message_is_skipped_not_fatal(db_session, offline_model):
    result = await commit_turn(
        conversation_id=uuid.uuid4(), user_id=SEEDED_USER_ID,
        assistant_message_id=uuid.uuid4(), mode="expert",
        session_factory=TestSessionLocal,
    )
    assert result.committed is False
    assert result.skipped_reason == "no_message"
