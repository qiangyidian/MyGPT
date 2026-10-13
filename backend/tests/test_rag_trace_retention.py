"""RAG trace 的留存清扫（文档 RAG-15）：过期就摘，且只摘回执。

这里钉三件事，每一件都有一个真实的坏法：

  * 过期的 ``rag_trace`` 必须真的消失 —— 否则「可追溯 N 天」是句空话，配置项
    就成了摆设开关；
  * ``validation`` 必须留下 —— 它是给用户看的那句「为什么拒答」，删了历史消息的
    角标会静默消失（那是产品行为，不是观测数据）；
  * ``days=0`` 表示关闭 —— 与其余留存项同一约定；把它读成「立刻删」会一夜之间
    抹掉所有人的排查线索。

**为什么自带一个库**：这一组用例断言的是「清掉了几条」，而 conftest 的
``TestSessionLocal`` 是整场共享的（用例之间不清表）。在共享库里，先前用例留下的行
会把计数搅乱 —— 那样的测试是顺序依赖的摆设，随机顺序一跑就红。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.services.retention import prune_rag_traces
from tests.conftest import _install_global_uuid_compilation


@pytest_asyncio.fixture
async def trace_db():
    """本模块专用的空库 + 会话工厂。"""
    _install_global_uuid_compilation()
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_message(factory, *, age_days: float, metadata: dict, role="assistant"):
    suffix = uuid.uuid4().hex[:10]
    async with factory() as db:
        user = User(
            email=f"trace-{suffix}@example.com",
            username=f"trace-{suffix}",
            password_hash="not-a-real-hash",
            role="user",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        conv = Conversation(user_id=user.id, title=f"conv-{suffix}")
        db.add(conv)
        await db.flush()
        msg = Message(
            conversation_id=conv.id,
            role=role,
            content="答案",
            metadata_=metadata,
        )
        db.add(msg)
        await db.flush()
        # created_at 有 Python 默认值（now），落库后再改，否则会被默认值覆盖回现在。
        msg.created_at = datetime.now(UTC) - timedelta(days=age_days)
        await db.commit()
        return msg.id


async def _metadata_of(factory, msg_id) -> dict:
    async with factory() as db:
        row = (await db.execute(select(Message).where(Message.id == msg_id))).scalar_one()
        return dict(row.metadata_ or {})


async def test_an_expired_trace_is_stripped(trace_db):
    msg_id = await _seed_message(
        trace_db,
        age_days=40,
        metadata={"rag_trace": {"trace_id": "tr-1"}, "validation": {"status": "answered"}},
    )
    assert await prune_rag_traces(trace_db, days=30) == 1
    md = await _metadata_of(trace_db, msg_id)
    assert "rag_trace" not in md
    # 给用户看的那部分必须留下。
    assert md["validation"] == {"status": "answered"}


async def test_a_fresh_trace_is_kept(trace_db):
    msg_id = await _seed_message(trace_db, age_days=1, metadata={"rag_trace": {"trace_id": "tr-2"}})
    assert await prune_rag_traces(trace_db, days=30) == 0
    assert "rag_trace" in await _metadata_of(trace_db, msg_id)


async def test_zero_days_means_off_not_delete_everything(trace_db):
    msg_id = await _seed_message(
        trace_db, age_days=4000, metadata={"rag_trace": {"trace_id": "tr-3"}}
    )
    assert await prune_rag_traces(trace_db, days=0) == 0
    assert "rag_trace" in await _metadata_of(trace_db, msg_id)


async def test_a_message_without_a_trace_is_not_counted(trace_db):
    """没有 trace 的消息不该被算成「清理过」—— 否则日志里那个数字是假的。"""
    await _seed_message(trace_db, age_days=40, metadata={"intent": {"mode": "speed"}})
    assert await prune_rag_traces(trace_db, days=30) == 0


async def test_a_user_message_is_never_touched(trace_db):
    msg_id = await _seed_message(
        trace_db, age_days=40, metadata={"rag_trace": {"trace_id": "tr-4"}}, role="user"
    )
    assert await prune_rag_traces(trace_db, days=30) == 0
    assert "rag_trace" in await _metadata_of(trace_db, msg_id)


async def test_one_batch_is_bounded(trace_db):
    """每轮最多处理 batch 条，避免一次扫全表把库锁住。"""
    for _ in range(3):
        await _seed_message(trace_db, age_days=40, metadata={"rag_trace": {"trace_id": "x"}})
    assert await prune_rag_traces(trace_db, days=30, batch=2) == 2
    # 剩下的下一轮再摘（这里不再等一轮，直接再跑一次确认它还在）。
    assert await prune_rag_traces(trace_db, days=30) == 1
