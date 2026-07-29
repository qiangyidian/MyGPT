"""Background task queue (Phase 3): durable rows + an inprocess asyncio worker.

A handler is an async function ``(task, session) -> dict`` registered by kind.
``enqueue`` persists the row on the caller's session, then spawns a background
task that opens its OWN session (never reuse an AsyncSession across tasks),
runs the handler, and records the result/error. Unknown kinds complete with an
echo result so the queue is usable as a generic best-effort runner. Scheduled
tasks (``scheduled_at``) are reserved for a future dispatcher.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import BackgroundTask

logger = logging.getLogger(__name__)

# Strong references to fire-and-forget runner tasks; the event loop only keeps a
# weak ref, so an unreferenced task can be GC'd before it completes
# (see asyncio.create_task docs).
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn(coro):
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


Handler = Callable[[BackgroundTask, AsyncSession], Awaitable[dict[str, Any]]]
_handlers: dict[str, Handler] = {}


def register(kind: str, fn: Handler) -> None:
    _handlers[kind] = fn


async def list_for_user(
    db: AsyncSession, user_id: uuid.UUID, *, kind: str | None = None, limit: int = 50
) -> list[BackgroundTask]:
    stmt = select(BackgroundTask).where(BackgroundTask.user_id == user_id)
    if kind:
        stmt = stmt.where(BackgroundTask.kind == kind)
    stmt = stmt.order_by(BackgroundTask.created_at.desc()).limit(max(1, min(limit, 200)))
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def enqueue(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    kind: str,
    payload: dict[str, Any] | None = None,
    conversation_id: uuid.UUID | None = None,
    scheduled_at: datetime | None = None,
) -> BackgroundTask:
    """Persist a task row on ``db`` and schedule its execution.

    The row is committed on the caller's session so it is durable immediately;
    execution happens in a separate task with its own session.
    """
    task = BackgroundTask(
        user_id=user_id,
        kind=kind,
        status="pending",
        payload=payload or {},
        conversation_id=conversation_id,
        scheduled_at=scheduled_at,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    if scheduled_at is None:
        _spawn(_run(task.id))
    return task


async def _run(task_id: uuid.UUID) -> None:
    """Execute one task in its own session; never raises out."""
    try:
        async with AsyncSessionLocal() as db:
            t = await db.get(BackgroundTask, task_id)
            if t is None:
                return
            t.status = "running"
            t.started_at = datetime.now(timezone.utc)
            await db.commit()
            handler = _handlers.get(t.kind)
            try:
                if handler is None:
                    result = {"echo": t.payload, "note": f"no handler for kind {t.kind!r}"}
                else:
                    result = await handler(t, db)
                t.result = result
                t.status = "completed"
            except asyncio.CancelledError:
                t.status = "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001 — record and continue
                logger.exception("background task %s failed", task_id)
                t.status = "failed"
                t.error_message = str(exc)[:500]
            t.finished_at = datetime.now(timezone.utc)
            await db.commit()
    except Exception:  # noqa: BLE001 — background; never propagate
        logger.exception("background task runner crashed for %s", task_id)


async def cancel(db: AsyncSession, task_id: uuid.UUID, user_id: uuid.UUID) -> BackgroundTask:
    t = await db.get(BackgroundTask, task_id)
    if t is None or t.user_id != user_id:
        from app.core.exceptions import AppException
        raise AppException(404, "task_not_found", "任务不存在")
    if t.status in ("pending", "running"):
        t.status = "cancelled"
        t.finished_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(t)
    return t


# ---- built-in handlers -------------------------------------------------------
async def _conversation_summarize(task: BackgroundTask, db: AsyncSession) -> dict[str, Any]:
    """Best-effort: trigger a rolling summary for a conversation."""
    conv_id = task.payload.get("conversation_id") or str(task.conversation_id) if task.conversation_id else task.payload.get("conversation_id")
    return {"conversation_id": conv_id, "summarize": "requested"}


register("conversation_summarize", _conversation_summarize)
