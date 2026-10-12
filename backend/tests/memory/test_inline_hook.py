"""inline 路径：一轮对话结束后恰好提交一次记忆（spec §1.2-1 的回归）。"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.conftest import auth_headers
from tests.memory.helpers import (
    conversation_of_message,
    create_mock_model,
    meta_from_sse,
    no_crew,
    wait_for_commit,
)


async def _drain_bg_tasks(timeout: float = 5.0) -> None:
    """等 chat_service 的 fire-and-forget 任务跑完，**期间绝不开任何 session**。

    测试库是 StaticPool（全进程共用一条 sqlite 连接）：任一个辅助 session 的关闭都会
    rollback 掉同一条连接上**另一个 session 未提交**的事务 —— 也就是正在跑的记忆写入
    （progress.md 记录的既有测试库特性）。所以顺序必须是"先等 detached 任务落地，再读
    库"；若改成一边用 helper 开 session 轮询一边等，记忆写入会被轮询的 rollback 抹掉，
    提交事件数恒为 0。
    """
    from app.services import chat_service

    async with asyncio.timeout(timeout):
        while chat_service._BG_TASKS:
            await asyncio.sleep(0.01)


async def _turn_conversation_id(response) -> uuid.UUID:
    """这一轮 API 对话建出的会话 id。

    会话由服务端创建，只能从 SSE 的 meta 反查（meta 里的 message_id 是 assistant
    消息，见 tests/test_chat_stream.py:120-123）。
    """
    meta = meta_from_sse(response.text)
    return await conversation_of_message(uuid.UUID(meta["message_id"]))


@pytest.mark.asyncio
async def test_inline_turn_commits_memory_once(client, offline_model, monkeypatch):
    # expert + 非寒暄内容会被路由到 deep_research crew，桩掉（见 helpers.no_crew）。
    no_crew(monkeypatch)
    headers = auth_headers()
    model_id = await create_mock_model(client, headers)
    response = await client.post(
        "/api/chat/stream",
        json={"content": "我喜欢用 Python 写脚本", "model_id": model_id, "mode": "expert"},
        headers=headers,
    )
    assert response.status_code == 200
    assert "event: done" in response.text

    # detached 任务是 fire-and-forget：先让它落库（这一步不碰库，见 _drain_bg_tasks），
    # 再按本会话断言。
    await _drain_bg_tasks()
    conversation_id = await _turn_conversation_id(response)
    assert await wait_for_commit(conversation_id) == 1


@pytest.mark.asyncio
async def test_speed_mode_also_commits(client, offline_model):
    """D4：极速不读，但后台仍写。"""
    headers = auth_headers()
    model_id = await create_mock_model(client, headers)
    response = await client.post(
        "/api/chat/stream",
        json={"content": "我喜欢用 Python 写脚本", "model_id": model_id, "mode": "speed"},
        headers=headers,
    )
    assert response.status_code == 200
    await _drain_bg_tasks()
    conversation_id = await _turn_conversation_id(response)
    assert await wait_for_commit(conversation_id) == 1
