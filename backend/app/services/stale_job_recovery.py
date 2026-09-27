"""Stale background-job recovery for chat attachments.

Attachment parsing runs as a fire-and-forget task inside whichever process
accepted the upload, so a restart destroys it and the row sits in
``pending``/``parsing`` forever — silently. This module re-enqueues such rows:

  * ``StaleJobSweeper`` — sweep, so a mid-parse crash heals without waiting for
    a deploy. Its first tick runs immediately when the loop starts, which is what
    makes a separate boot-time pass unnecessary. Runs on **one** process
    (leader-gated), exactly like the retention and recovery loops — see the class.

There used to be a second, ungated entry point: a one-shot pass in the API
lifespan. It is gone, because "one bounded pass per process start" is not one
pass per *deploy* — a compose/k8s rollout starts every replica at the same
instant, so N replicas meant N live parses of the same abandoned attachment,
and the dedup that would have caught it (:data:`_tasks`) is per-process. The
boot healing survives: whoever wins the lock sweeps on its very first tick.

It used to do the same for knowledge-base documents, and that half is gone on
purpose: document ingestion is a durable, leased queue now
(:mod:`app.services.ingestion_queue`), where a row whose worker died becomes
claimable the moment its lease expires. That heals faster (a lease TTL, not a
10-minute age threshold) and is safe to run in parallel, which this time
heuristic could not be — it re-dispatched rows a live worker was still indexing,
and the deduplication table below is per-process, so a second process could not
see it.

Idempotent and best-effort: never raises, and a row that keeps failing lands in
its normal ``failed`` terminal state via the existing parse error handling.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, UTC
from typing import Any

from sqlalchemy import select

logger = logging.getLogger(__name__)

# Rows stuck in a non-terminal state for longer than this are considered lost
# (the normal parse path takes seconds; 10 minutes is a generous ceiling).
_STALE_AFTER = timedelta(minutes=10)

# Upper bound per sweep so a huge backlog can't monopolize the loop.
_MAX_REQUEUE_PER_SWEEP = 50


async def requeue_stale_jobs_once(session_factory: Any) -> tuple[int, int]:
    """One pass: re-enqueue attachments abandoned by a previous process.

    Called only by :class:`StaleJobSweeper`, whose loop ticks while it holds the
    ``stale-jobs`` lock. This pass is not a candidate for "let every replica run
    it, the UPDATE is conditional anyway": the UPDATE claims the *status flip*,
    not the parse — the re-dispatch below executes in whichever process ran the
    query, so N concurrent passes mean N live parses of the same attachment.

    Returns ``(0, attachments)``. The leading zero is the vestigial document
    count callers log: documents are no longer this module's job, they are
    claimed off their ingestion lease.
    """
    atts = await _stale_attachments(session_factory)
    for attachment_id in atts:
        _schedule_attachment_parse(session_factory, attachment_id)
    if atts:
        logger.warning(
            "stale-job recovery: re-enqueued %d attachment(s) abandoned by a "
            "previous process",
            len(atts),
        )
    return 0, len(atts)


class StaleJobSweeper:
    """Periodic re-enqueue sweep (share the recovery scheduler's cadence).

    Leader-gated by default, i.e. the same source of truth as
    :class:`app.services.retention.RetentionSweeper` and the recovery scan
    (finding 39, see :mod:`app.core.leader`).

    Why it must run in exactly one process: this loop does not read state, it
    *rewrites* it. Every API replica used to start one, so a 3-replica deploy
    flipped the same expired attachments back to ``pending`` three times per
    interval and re-dispatched the same parse in three processes at once —
    wasted CPU and provider calls on the smallest box in the stack, three
    concurrent ``UPDATE ... WHERE`` batches deadlocking each other on Postgres,
    and three contradictory copies of "who pushed this row back to the queue,
    and when" in the log the operator reads after a bad parse. The conditional
    UPDATE is what let that survive review: it makes a duplicate loss-free, so
    the cost was never a dropped row, only the noise, the load and the audit
    trail nobody can trust. With ``leader_name`` set only the holder of the
    advisory lock sweeps, and a peer promotes within one heartbeat of the
    leader dying — gating costs availability, not coverage.

    Degradation matches the other gated loops: on a non-Postgres backend (the
    SQLite test suite, single-process dev) there is no advisory lock, so the
    gate reports leader and the sweep runs unconditionally rather than silently
    disappearing from local development. Pass ``leader_name=None`` to force
    unconditional running regardless of backend.
    """

    def __init__(
        self,
        session_factory: Any,
        interval_seconds: int = 300,
        *,
        leader_name: str | None = "stale-jobs",
    ) -> None:
        self._session_factory = session_factory
        self._interval = max(int(interval_seconds), 60)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # One gate per singleton loop, and the name *is* the lock identity (the
        # key is hashed from it), so "stale-jobs" must stay distinct from
        # "retention" / "recovery": sharing a name would mean sharing one slot,
        # and whichever loop grabbed it first would starve the other for the
        # whole lifetime of the process.
        self._gate = None
        if leader_name is not None:
            from app.core.leader import LeaderGate

            self._gate = LeaderGate(leader_name, session_factory=session_factory)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._gate is not None:
            # Hand the lock (and the pooled connection holding it) back on
            # shutdown: a standby otherwise waits out this process's whole
            # remaining lifetime before it can promote, which is exactly the
            # roll-restart window where a stale attachment needs healing.
            await self._gate.release()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                # A replica that does not hold the lock skips the tick outright
                # — no SELECT, no UPDATE, no re-dispatch. "Every replica sweeps
                # and the SQL is idempotent anyway" is the stance this gate
                # replaces (see the class docstring for what it cost).
                may_run = True if self._gate is None else await self._gate.acquire()
                if may_run:
                    await requeue_stale_jobs_once(self._session_factory)
            except Exception:
                logger.exception("stale-job sweep failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass


async def _stale_attachments(session_factory: Any) -> list[uuid.UUID]:
    from app.models.chat_attachment import ChatAttachment

    cutoff = datetime.now(UTC) - _STALE_AFTER
    async with session_factory() as db:
        result = await db.execute(
            select(ChatAttachment.id)
            .where(
                ChatAttachment.parse_status.in_(["pending", "parsing"]),
                ChatAttachment.created_at < cutoff,
            )
            .limit(_MAX_REQUEUE_PER_SWEEP)
        )
        ids = list(result.scalars().all())
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
