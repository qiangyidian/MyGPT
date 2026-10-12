"""durable 路径也产出记忆（spec §1.2-1 的核心回归）。

驱动方式抄自 tests/test_chat_stream.py:86-99：``background_worker`` 是 parametrize 的
字符串参数，durable 路径要自己起一个 worker 把 run 跑掉。
"""
from __future__ import annotations

import asyncio

import pytest

from tests.conftest import auth_headers
from tests.memory.helpers import create_mock_model, drive_durable_worker, no_crew, wait_for_commit
from tests.memory.test_inline_hook import _turn_conversation_id


@pytest.mark.asyncio
async def test_durable_turn_commits_memory(client, monkeypatch, offline_model):
    from app.agents.workflow.queue import InMemoryQueue, set_run_queue
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "BACKGROUND_WORKER", "durable")
    no_crew(monkeypatch)   # expert + 非寒暄内容会拉起 research crew，见 helpers.no_crew
    headers = auth_headers()
    model_id = await create_mock_model(client, headers)

    queue = InMemoryQueue()
    set_run_queue(queue)
    worker_task = asyncio.create_task(drive_durable_worker(queue))
    try:
        response = await client.post(
            "/api/chat/stream",
            json={"content": "我在杭州做后端，记住这点", "model_id": model_id, "mode": "expert"},
            headers=headers,
        )
        assert response.status_code == 200
        await worker_task
    finally:
        if not worker_task.done():
            worker_task.cancel()

    conversation_id = await _turn_conversation_id(response)
    assert await wait_for_commit(conversation_id) == 1
