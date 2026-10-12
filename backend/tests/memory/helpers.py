"""记忆测试共用的夹具函数。

``seeded_db`` 只建表 + 播种一个固定 id 的用户，不建会话 —— 要会话就在这里建。
durable 路径的驱动方式抄自 ``tests/test_chat_stream.py:86-99``（``background_worker``
是 parametrize 的字符串，不是夹具）。

**内存库是全进程共享的**：``seeded_db`` 是 session 级夹具 + StaticPool，同一进程里所有
测试落在同一个物理库上。所以断言一律**按本测试自己的会话/内容限定**，绝不写全局计数
（既有测试同样按实体断言，见 ``tests/test_chat_stream.py:120-123``）。
"""
from __future__ import annotations

import asyncio
import json
import uuid

# tests/conftest.py:210 播种的普通用户，id 是固定的（测试可直接引用，不必查库）。
SEEDED_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def create_conversation(db, *, user_id: uuid.UUID = SEEDED_USER_ID) -> uuid.UUID:
    from app.models import Conversation

    conv = Conversation(user_id=user_id, title="记忆测试会话")
    db.add(conv)
    await db.flush()
    return conv.id


async def add_message(db, conversation_id: uuid.UUID, role: str, content: str, *, created_at=None):
    from app.models import Message

    msg = Message(conversation_id=conversation_id, role=role, content=content)
    if created_at is not None:
        msg.created_at = created_at
    db.add(msg)
    await db.flush()
    return msg.id


async def create_mock_model(client, headers) -> str:
    """建一个走 MockProvider 的模型配置（与 tests/test_chat_stream.py:15-30 同款）。"""
    r = await client.post(
        "/api/models",
        json={
            "name": "Mock for memory",
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


async def drive_durable_worker(queue, timeout: float = 10.0) -> None:
    """等队列出现 pending 后跑一次 worker（durable 路径的唯一驱动方式）。"""
    from app.agents.workflow.execution import execute_run
    from app.agents.workflow.worker import RunWorker
    from tests.conftest import TestSessionLocal

    async with asyncio.timeout(timeout):
        while not await queue.pending_ids():
            await asyncio.sleep(0.01)
        await RunWorker(
            queue=queue, execute_fn=execute_run, session_factory=TestSessionLocal
        ).run_once()


async def commit_events(conversation_id: uuid.UUID) -> list:
    """**该会话**的 commit_turn 事件，返回普通行元组（脱离 session 后仍可读）。"""
    from sqlalchemy import select

    from app.models import MemoryEvent
    from tests.conftest import TestSessionLocal

    async with TestSessionLocal() as session:
        rows = await session.execute(
            select(MemoryEvent.id, MemoryEvent.dedup_key).where(
                MemoryEvent.kind == "commit_turn",
                MemoryEvent.conversation_id == conversation_id,
            )
        )
        return list(rows.all())


async def wait_for_commit(conversation_id: uuid.UUID, timeout: float = 5.0) -> int:
    """等 detached 记忆任务落库，返回**该会话**的 commit_turn 事件数。"""
    deadline = asyncio.get_running_loop().time() + timeout
    n = 0
    while asyncio.get_running_loop().time() < deadline:
        n = len(await commit_events(conversation_id))
        if n:
            return n
        await asyncio.sleep(0.1)
    return n


def meta_from_sse(text: str) -> dict:
    """取 SSE 正文里第一条 ``data:`` 行解析出的 meta（抄 tests/test_chat_stream.py:116-119）。"""
    return json.loads(
        next(
            line.removeprefix("data: ")
            for line in text.splitlines()
            if line.startswith("data:")
        )
    )


async def conversation_of_message(message_id: uuid.UUID) -> uuid.UUID:
    """一条消息属于哪个会话（API 驱动的一轮里，会话是服务端建的）。"""
    from app.models import Message
    from tests.conftest import TestSessionLocal

    async with TestSessionLocal() as session:
        msg = await session.get(Message, message_id)
        assert msg is not None, f"消息 {message_id} 不存在"
        return msg.conversation_id


def no_crew(monkeypatch) -> None:
    """让 expert 模式的一轮不拉起 deep_research crew（抄 tests/test_chat_stream.py:59-77）。

    ``CREWAI_ENABLED`` 默认 True 且测试环境没有关它，而 expert + 非寒暄内容会被
    ``decide_route`` 路由到 ``use_multi_agent=True`` 的 research crew —— 不桩住的话每个
    用例都要等一个多 Agent 流程在 MockProvider 上跑完（慢且不确定）。桩成"crew 不可用"
    后编排器走原生回答，与 test_chat_stream 的既有做法一致。
    """
    from unittest.mock import AsyncMock, Mock

    from app.agents.intent_service import intent_service
    from app.agents.orchestrator import ChatOrchestrator

    monkeypatch.setattr(intent_service, "judge", AsyncMock(return_value=None))
    monkeypatch.setattr(
        ChatOrchestrator, "_crewai_status", Mock(return_value=(False, "unexpected_crew"))
    )


async def user_memories_for_conversation(conversation_id: uuid.UUID) -> list:
    """该会话产出的候选，返回 ``(id, content, active, source)`` 普通行元组。"""
    from sqlalchemy import select

    from app.models import UserMemory
    from tests.conftest import TestSessionLocal

    async with TestSessionLocal() as session:
        rows = await session.execute(
            select(UserMemory.id, UserMemory.content, UserMemory.active, UserMemory.source).where(
                UserMemory.source_conversation_id == conversation_id
            )
        )
        return list(rows.all())
