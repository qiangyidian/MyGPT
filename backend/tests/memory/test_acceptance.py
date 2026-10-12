"""S1 验收（spec §13 在 S1 范围内的可判定条目）。"""
from __future__ import annotations

import pytest

from tests.conftest import TestSessionLocal, auth_headers
from tests.memory.helpers import (
    SEEDED_USER_ID,
    add_message,
    commit_events,
    create_conversation,
    create_mock_model,
    no_crew,
    user_memories_for_conversation,
    wait_for_commit,
)
from tests.memory.test_inline_hook import _drain_bg_tasks, _turn_conversation_id

# 每个测试（以及同一测试的每个 parametrize 分支）必须用**互不相同**的候选内容：
# propose_candidate 的去重键是 (user_id, content) —— 全局按用户去重，不按会话。同一个
# seeded 用户 + 同一句话，第二个测试的插入会被去重成 0 条，于是"本会话应有候选"的断言
# 随机失败（T4 实现时已经踩到过这个坑）。
_CONTENT_BY_MODE = {"speed": "我喜欢用 Go 写服务", "expert": "我喜欢用 Python 写脚本"}


@pytest.mark.parametrize("mode", ["speed", "expert"])
@pytest.mark.asyncio
async def test_candidates_are_never_active_in_s1(client, offline_model, mode, monkeypatch):
    """S1 不改治理：候选一律 inactive（显式"记住"的自动生效在 S2）。"""
    no_crew(monkeypatch)   # expert 分支会拉起 research crew，见 helpers.no_crew
    headers = auth_headers()
    model_id = await create_mock_model(client, headers)
    response = await client.post(
        "/api/chat/stream",
        json={"content": _CONTENT_BY_MODE[mode], "model_id": model_id, "mode": mode},
        headers=headers,
    )
    assert response.status_code == 200
    # 必须先排空 fire-and-forget 任务，再读库（**`_turn_conversation_id` 本身就是一次读库**，
    # 它内部的辅助 session 一关就会 rollback）：测试库是 StaticPool（全进程一条连接），
    # 读回用的辅助 session 一关就会 rollback 掉 detached 写入未提交的事务（T5/T8 踩过）。
    await _drain_bg_tasks()
    conversation_id = await _turn_conversation_id(response)
    assert await wait_for_commit(conversation_id) == 1

    rows = await user_memories_for_conversation(conversation_id)
    assert rows, "两种模式都该产出候选（极速：不读 ≠ 不写）"
    assert all(active is False for _id, _content, active, _source in rows)
    assert all(source == "inferred" for _id, _content, _active, source in rows)


@pytest.mark.asyncio
async def test_same_turn_never_double_commits(client, offline_model, monkeypatch):
    """sweeper 与 detached 任务并发也不会重复：幂等锚点在数据库层。"""
    from app.agents.memory.sweeper import MemorySweeper

    no_crew(monkeypatch)   # expert + 非寒暄内容会拉起 research crew，见 helpers.no_crew
    headers = auth_headers()
    model_id = await create_mock_model(client, headers)
    response = await client.post(
        "/api/chat/stream",
        json={"content": "我喜欢用 Rust 写工具", "model_id": model_id, "mode": "expert"},
        headers=headers,
    )
    assert response.status_code == 200
    await _drain_bg_tasks()   # 先排空 detached 写入，再读库（含 _turn_conversation_id；StaticPool 回滚陷阱）
    conversation_id = await _turn_conversation_id(response)
    assert await wait_for_commit(conversation_id) == 1

    # 兜底扫描再跑一遍，本会话仍应只有一条提交事件（全局返回值不作断言：
    # 扫描面包含同进程其它测试留下的会话）。
    sweeper = MemorySweeper(TestSessionLocal, leader_name=None, interval_seconds=600)
    await sweeper.run_once()
    assert len(await commit_events(conversation_id)) == 1


@pytest.mark.asyncio
async def test_memory_side_calls_do_not_touch_billing(db_session, offline_model, monkeypatch):
    """G7：记忆侧调用不写 usage、不计 credit。"""
    from app.agents.memory.facade import commit_turn
    from app.services import chat_service

    calls: list = []
    monkeypatch.setattr(
        chat_service, "settle_turn_usage", lambda *a, **k: calls.append((a, k)), raising=True
    )

    conversation_id = await create_conversation(db_session)
    await add_message(db_session, conversation_id, "user", "我喜欢用 Python 写脚本")
    assistant_id = await add_message(db_session, conversation_id, "assistant", "好的")
    await db_session.commit()

    await commit_turn(
        conversation_id=conversation_id, user_id=SEEDED_USER_ID,
        assistant_message_id=assistant_id, mode="expert",
        session_factory=TestSessionLocal,
    )
    assert calls == [], "记忆写入不该触发计费结算"


@pytest.mark.asyncio
async def test_candidate_endpoint_contract_unchanged(client, db_session, offline_model):
    """现有 /api/memories 契约不变：新列只是 additive，旧字段语义不变。"""
    headers = auth_headers()
    r = await client.post(
        "/api/memories",
        json={"content": "用户手写的偏好", "memory_type": "preference"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["active"] is False            # 手写候选仍需激活
    assert body["content"] == "用户手写的偏好"
