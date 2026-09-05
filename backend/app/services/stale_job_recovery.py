"""Stale background-job recovery for documents + chat attachments.

Document indexing and attachment parsing run as fire-and-forget background
tasks inside whichever process accepted the upload. A process restart (deploy,
crash) destroys those tasks and the rows sit in ``pending``/``parsing``
forever — the knowledge base silently stopped working and nothing told the
user. This module re-enqueues such rows:

  * ``requeue_stale_jobs_once`` — called once at API boot (lifespan);
  * ``StaleJobSweeper`` — periodic sweep (runs inside the recovery process /
    recovery scheduler loop) so a mid-parse crash between boots also heals.

Both paths are idempotent and best-effort: they never raise, and a row that
fails re-parsing lands in its normal ``failed`` terminal state via the
existing parse/index error handling.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

logger = logging.getLogger(__name__)

# Rows stuck in a non-terminal state for longer than this are considered lost
# (the normal parse path takes seconds; 10 minutes is a generous ceiling).
_STALE_AFTER = timedelta(minutes=10)

# Upper bound per sweep so a huge backlog can't monopolize the loop.
_MAX_REQUEUE_PER_SWEEP = 50


async def requeue_stale_jobs_once(session_factory: Any) -> tuple[int, int]:
    """Boot-time sweep: re-enqueue documents + attachments lost to a restart.

    Returns ``(documents, attachments)`` re-enqueued counts (for the log line).
    """
    docs = await _stale_documents(session_factory)
    for document_id in docs:
        _schedule_document_index(session_factory, document_id)
    atts = await _stale_attachments(session_factory)
    for attachment_id in atts:
        _schedule_attachment_parse(session_factory, attachment_id)
    if docs or atts:
        logger.warning(
            "stale-job recovery: re-enqueued %d document(s) + %d attachment(s) "
            "abandoned by a previous process",
            len(docs), len(atts),
        )
    return len(docs), len(atts)


class StaleJobSweeper:
    """Periodic re-enqueue sweep (share the recovery scheduler's cadence)."""

    def __init__(self, session_factory: Any, interval_seconds: int = 300) -> None:
        self._session_factory = session_factory
        self._interval = max(int(interval_seconds), 60)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await requeue_stale_jobs_once(self._session_factory)
            except Exception:
                logger.exception("stale-job sweep failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass


async def _stale_documents(session_factory: Any) -> list[uuid.UUID]:
    from app.models.document import Document

    cutoff = datetime.now(timezone.utc) - _STALE_AFTER
    async with session_factory() as db:
        result = await db.execute(
            select(Document.id)
            .where(
                Document.status.in_(["pending", "parsing"]),
                Document.created_at < cutoff,
            )
            .limit(_MAX_REQUEUE_PER_SWEEP)
        )
        ids = [row for row in result.scalars().all()]
        if ids:
            # Flip parsing → pending so the index task starts clean.
            await db.execute(
                Document.__table__.update()
                .where(Document.id.in_(ids), Document.status == "parsing")
                .values(status="pending")
            )
            await db.commit()
        return list(ids)


async def _stale_attachments(session_factory: Any) -> list[uuid.UUID]:
    from app.models.chat_attachment import ChatAttachment

    cutoff = datetime.now(timezone.utc) - _STALE_AFTER
    async with session_factory() as db:
        result = await db.execute(
            select(ChatAttachment.id)
            .where(
                ChatAttachment.parse_status.in_(["pending", "parsing"]),
                ChatAttachment.created_at < cutoff,
            )
            .limit(_MAX_REQUEUE_PER_SWEEP)
        )
        ids = [row for row in result.scalars().all()]
        if ids:
            await db.execute(
                ChatAttachment.__table__.update()
                .where(
                    ChatAttachment.id.in_(ids),
                    ChatAttachment.parse_status == "parsing",
                )
                .values(parse_status="pending")
            )
            await db.commit()
        return list(ids)


def _schedule_document_index(session_factory: Any, document_id: uuid.UUID) -> None:
    async def _run() -> None:
        try:
            from app.services.document_service import index_document

            async with session_factory() as db:
                await index_document(db, document_id)
        except Exception:
            logger.exception("stale-job recovery: reindex failed for %s", document_id)

    _spawn(f"doc-index-{document_id}", _run())


def _schedule_attachment_parse(session_factory: Any, attachment_id: uuid.UUID) -> None:
    async def _run() -> None:
        try:
            from app.services.attachment_service import parse_attachment_now

            await parse_attachment_now(session_factory, attachment_id)
        except Exception:
            logger.exception("stale-job recovery: re-parse failed for %s", attachment_id)

    _spawn(f"attachment-parse-{attachment_id}", _run())


_tasks: dict[str, asyncio.Task] = {}


def _spawn(name: str, coro) -> None:
    """Schedule a tracked background task (strong reference, deduped by name)."""
    existing = _tasks.get(name)
    if existing is not None and not existing.done():
        existing.cancel()
    task = asyncio.create_task(coro)
    _tasks[name] = task
    task.add_done_callback(_tasks.pop, name)
