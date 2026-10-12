"""三层记忆 schema：新列、唯一键、幂等锚点（spec §5.1 / §6.1 / §9.6）。"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import ConversationMemory, MemoryEvent, MemorySettings, UserMemory
from tests.memory.helpers import SEEDED_USER_ID, create_conversation


@pytest.mark.asyncio
async def test_user_memory_has_new_columns(db_session):
    row = UserMemory(
        user_id=SEEDED_USER_ID,
        memory_type="preference",
        content="用户偏好用 Python",
        source="explicit",
        author="extractor",
        importance=0.8,
    )
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)          # server_default 在 SQLite 上未必回填到实例
    assert row.valid_from is not None
    assert row.invalid_at is None
    assert row.superseded_by is None
    assert row.use_count == 0


@pytest.mark.asyncio
async def test_conversation_memory_block_key_is_unique(db_session):
    conversation_id = await create_conversation(db_session)
    db_session.add(
        ConversationMemory(
            conversation_id=conversation_id, user_id=SEEDED_USER_ID,
            memory_type="task", block_key="goal", content="第一版目标",
        )
    )
    await db_session.flush()
    db_session.add(
        ConversationMemory(
            conversation_id=conversation_id, user_id=SEEDED_USER_ID,
            memory_type="task", block_key="goal", content="第二版目标",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_two_null_block_keys_do_not_conflict(db_session):
    """历史行（block_key 为 NULL）不互撞唯一键 —— Postgres 的 NULL 互不相等。"""
    conversation_id = await create_conversation(db_session)
    for content in ("历史一", "历史二"):
        db_session.add(
            ConversationMemory(
                conversation_id=conversation_id, user_id=SEEDED_USER_ID,
                memory_type="fact", block_key=None, content=content,
            )
        )
    await db_session.flush()   # 不抛即通过


@pytest.mark.asyncio
async def test_commit_turn_event_is_unique_per_message(db_session):
    conversation_id = await create_conversation(db_session)
    message_id = uuid.uuid4()
    key = MemoryEvent.commit_key(conversation_id, message_id)
    for _ in range(2):
        db_session.add(
            MemoryEvent(
                user_id=SEEDED_USER_ID, conversation_id=conversation_id,
                message_id=message_id, kind="commit_turn", payload={}, dedup_key=key,
            )
        )
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_other_event_kinds_are_not_unique(db_session):
    """唯一键只约束带 dedup_key 的行：同一轮的 rejected / noop 可以有多条。

    这条同时锁住"不许退回 partial unique index"——partial index 在测试库（SQLite）上会
    退化成对 (conversation_id, message_id) 的无条件唯一，本用例会失败。
    """
    conversation_id = await create_conversation(db_session)
    message_id = uuid.uuid4()
    for _ in range(2):
        db_session.add(
            MemoryEvent(
                user_id=SEEDED_USER_ID, conversation_id=conversation_id,
                message_id=message_id, kind="noop", payload={}, dedup_key=None,
            )
        )
    await db_session.flush()   # 不抛即通过


@pytest.mark.asyncio
async def test_memory_settings_has_no_default_row(db_session):
    """缺行 = 全默认开启：库里不预造行，由读取方解释。"""
    row = (
        await db_session.execute(
            select(MemorySettings).where(MemorySettings.user_id == SEEDED_USER_ID)
        )
    ).scalar_one_or_none()
    assert row is None
