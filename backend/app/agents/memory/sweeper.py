"""补做漏掉的回合记忆提交（spec §4.2.1）。

为什么需要它：inline 路径的 detached 任务会随 API 进程重启丢掉，durable run 也可能在
写记忆之前崩掉。丢一条候选的代价很小，但"长期一条都不记"是系统性的 —— 所以要有兜底
扫描。

模式与 ``services/retention.py::RetentionSweeper`` 完全一致：leader-gated，API 与
worker 各起一个，Postgres advisory lock 保证只有一个真跑。跟随它的理由也一样 —— 多
副本下重复跑同一批 UPDATE 会互相死锁。

**回溯窗口有限**（``MEMORY_SWEEP_WINDOW_HOURS``）：补做是兜底，不是全量重算 ——
无边界扫描会在一次事故后攒出一大批待补回合，把预算一次打光。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import exists, select

from app.core.config import get_settings
from app.models import Conversation, MemoryEvent, Message

logger = logging.getLogger(__name__)


class MemorySweeper:
    def __init__(
        self,
        session_factory: Any,
        interval_seconds: int = 15 * 60,
        *,
        leader_name: str | None = "memory",
    ) -> None:
        self._session_factory = session_factory
        self._interval = max(int(interval_seconds), 600)
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._gate = None
        if leader_name is not None:
            from app.core.leader import LeaderGate

            self._gate = LeaderGate(leader_name, session_factory=session_factory)

    def start(self) -> None:
        if self._task is None:
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._gate is not None:
            await self._gate.release()

    async def _loop(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                may_run = True if self._gate is None else await self._gate.acquire()
                if may_run:
                    await self.run_once()
            except Exception:
                logger.exception("memory sweep failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass

    async def run_once(self) -> int:
        """补做一批，返回本次提交的回合数。"""
        from app.agents.memory.facade import commit_turn

        settings = get_settings()
        since = datetime.now(UTC) - timedelta(hours=settings.MEMORY_SWEEP_WINDOW_HOURS)

        async with self._session_factory() as db:
            # 每个会话"窗口内最新的 assistant 消息"，且没有对应的 commit_turn 事件。
            latest_id = (
                select(Message.id)
                .where(
                    Message.conversation_id == Conversation.id,
                    Message.role == "assistant",
                    Message.created_at >= since,
                )
                .order_by(Message.created_at.desc())
                .limit(1)
                .correlate(Conversation)
                .scalar_subquery()
            )
            has_event = exists(
                select(MemoryEvent.id).where(
                    MemoryEvent.kind == "commit_turn",
                    MemoryEvent.conversation_id == Conversation.id,
                    MemoryEvent.message_id == latest_id,
                )
            )
            rows = (
                await db.execute(
                    select(Conversation.id, Conversation.user_id, latest_id.label("message_id"))
                    .where(latest_id.is_not(None), ~has_event)
                    .limit(settings.MEMORY_SWEEP_BATCH)
                )
            ).all()

        done = 0
        for conversation_id, user_id, message_id in rows:
            try:
                result = await commit_turn(
                    conversation_id=conversation_id,
                    user_id=user_id,
                    assistant_message_id=message_id,
                    # 补做一律按最全的写入面：判定"该不该写"的是 gate.write，而只有
                    # hermes 为 False；模式在这里不影响写入。
                    mode="expert",
                    cfg_id=None,
                    session_factory=self._session_factory,
                )
            except Exception:
                logger.warning("memory sweep commit failed", exc_info=True)
                continue
            if result.committed and result.skipped_reason is None:
                done += 1
        if done:
            logger.info("memory sweep: committed %d missed turn(s)", done)
        return done
