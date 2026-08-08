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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import BackgroundTask

logger = logging.getLogger(__name__)

# Strong references to fire-and-forget runner tasks; the event loop only keeps a
# weak ref, so an unreferenced task can be GC'd before it completes
# (see asyncio.create_task docs).
_BACKGROUND_TASKS: set[asyncio.Task] = set()
# Live runners keyed by BackgroundTask.id so cancel() can actually interrupt the
# in-flight handler (not just flip the DB row). Cleaned up in the done callback.
_RUNNERS: dict[uuid.UUID, asyncio.Task] = {}


def _spawn_runner(task_id: uuid.UUID) -> asyncio.Task:
    """Schedule _run(task_id) and track it by id for cancellation."""
    task = asyncio.create_task(_run(task_id))
    _BACKGROUND_TASKS.add(task)
    _RUNNERS[task_id] = task

    def _done(t: asyncio.Task, tid: uuid.UUID = task_id) -> None:
        _BACKGROUND_TASKS.discard(t)
        _RUNNERS.pop(tid, None)

    task.add_done_callback(_done)
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
        _spawn_runner(task.id)
    return task


async def _run(task_id: uuid.UUID) -> None:
    """Execute one task in its own session; never raises out.

    Status transitions are guarded by conditional UPDATEs (``WHERE status=<prev>``)
    so a concurrent ``cancel()`` that already flipped the row to ``cancelled`` on
    another session is never clobbered back to ``completed``/``failed`` by this
    runner finishing a moment later.
    """
    try:
        async with AsyncSessionLocal() as db:
            t = await db.get(BackgroundTask, task_id)
            if t is None:
                return
            # pending -> running, but only if still pending (a pre-spawn cancel wins).
            started = await db.execute(
                update(BackgroundTask)
                .where(BackgroundTask.id == task_id, BackgroundTask.status == "pending")
                .values(status="running", started_at=datetime.now(timezone.utc))
            )
            await db.commit()
            if started.rowcount == 0:
                return  # already cancelled/finished before we got the runner
            handler = _handlers.get(t.kind)
            new_status = "completed"
            result: dict[str, Any] | None = None
            error_message: str | None = None
            try:
                if handler is None:
                    result = {"echo": t.payload, "note": f"no handler for kind {t.kind!r}"}
                else:
                    result = await handler(t, db)
            except asyncio.CancelledError:
                new_status = "cancelled"
            except Exception as exc:  # noqa: BLE001 — record and continue
                logger.exception("background task %s failed", task_id)
                new_status = "failed"
                error_message = str(exc)[:500]
            # Conditional finalize: only flip while still 'running', so a cancel()
            # that already wrote 'cancelled' is never overwritten.
            values: dict[str, Any] = {"status": new_status, "finished_at": datetime.now(timezone.utc)}
            if result is not None:
                values["result"] = result
            if error_message is not None:
                values["error_message"] = error_message
            await db.execute(
                update(BackgroundTask)
                .where(BackgroundTask.id == task_id, BackgroundTask.status == "running")
                .values(**values)
            )
            await db.commit()
    except asyncio.CancelledError:
        # Cancelled mid-finalize: the conditional UPDATE above already recorded
        # 'cancelled' (or cancel() did on its own session); nothing more to do.
        logger.info("background task %s runner cancelled", task_id)
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
    # Actually interrupt the live runner so a long handler stops now (not just
    # the next status poll). Best-effort: the conditional UPDATE in _run already
    # guarantees the 'cancelled' status survives even if the runner finishes.
    runner = _RUNNERS.get(task_id)
    if runner is not None and not runner.done():
        runner.cancel()
    return t


# ---- built-in handlers -------------------------------------------------------
async def _conversation_summarize(task: BackgroundTask, db: AsyncSession) -> dict[str, Any]:
    """Best-effort: trigger a rolling summary for a conversation."""
    conv_id = task.payload.get("conversation_id") or str(task.conversation_id) if task.conversation_id else task.payload.get("conversation_id")
    return {"conversation_id": conv_id, "summarize": "requested"}


register("conversation_summarize", _conversation_summarize)
