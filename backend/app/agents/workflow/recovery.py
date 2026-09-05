"""Stale-run recovery scheduler (Task 5).

The recovery scheduler reconciles runs whose owning worker died mid-execution.
A dead worker leaves behind either:

  * an **expired lease** (``run_leases.expires_at`` in the past) for a run still
    in a non-terminal status, or
  * a **legacy running row** (``agent_runs.status = 'running'``) with NO lease
    at all (a run created before leases existed, or one where the lease row was
    lost).

``scan()`` is **idempotent**: a requeued run gets a fresh lease (so the next
scan sees it as live), and a terminally-failed run is terminal (so the next
scan skips it). Calling scan() twice in a row returns the first batch then
``[]`` until new leases expire.

Retry budget: the count of ``recovery.requeued`` events in the run's event log
IS the retry counter — no extra column or migration needed. After
``max_retries`` the run is marked ``failed`` with an explicit reason.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.events import EventStore
from app.agents.workflow.queue import RunQueue
from app.core.config import get_settings
from app.models.agent_run import AgentRun
from app.models.run_lease import RunLease

logger = logging.getLogger(__name__)

_NON_TERMINAL = ("pending", "running", "waiting_approval", "paused")
_RECOVERY_EVENT = "recovery.requeued"


def _is_expired(expires_at: datetime) -> bool:
    """True when ``expires_at`` is at/before now (normalises naive datetimes)."""
    now = datetime.now(UTC)
    exp = expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return now >= exp


class RecoveryScheduler:
    """Reconcile expired/stale leases: requeue retryable runs, fail exhausted ones."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        queue: RunQueue,
        max_retries: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._queue = queue
        self._max_retries = (
            max_retries
            if max_retries is not None
            else get_settings().RUN_MAX_RETRIES
        )

    async def scan(self) -> list[uuid.UUID]:
        """One recovery pass. Returns the run_ids acted on (requeued or failed).

        Idempotent: a second call returns ``[]`` until a NEW lease expires,
        because requeued runs get a fresh lease and failed runs are terminal.
        """
        acted: list[uuid.UUID] = []
        async with self._session_factory() as session:
            expired = await self._find_expired_leases(session)
            stale_no_lease = await self._find_stale_running_no_lease(session)

        # Merge the two sets, dedup preserving order.
        seen: set[uuid.UUID] = set()
        candidates: list[tuple[uuid.UUID, uuid.UUID]] = []  # (run_id, lease_id|None)
        for lease_id, run_id in expired:
            if run_id not in seen:
                seen.add(run_id)
                candidates.append((run_id, lease_id))
        for run_id in stale_no_lease:
            if run_id not in seen:
                seen.add(run_id)
                candidates.append((run_id, None))

        for run_id, lease_id in candidates:
            action = await self._reconcile(run_id, lease_id)
            if action:
                acted.append(run_id)
        return acted

    # ------------------------------------------------------------------ #
    async def _find_expired_leases(
        self, session: AsyncSession
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """Return ``(lease_id, run_id)`` for leases that have expired AND whose
        run is still in a non-terminal status."""
        result = await session.execute(select(RunLease))
        expired: list[tuple[uuid.UUID, uuid.UUID]] = []
        for lease in result.scalars().all():
            if not _is_expired(lease.expires_at):
                continue
            run = await session.get(AgentRun, lease.run_id)
            if run is None or run.status not in _NON_TERMINAL:
                continue
            expired.append((lease.id, lease.run_id))
        return expired

    async def _find_stale_running_no_lease(
        self, session: AsyncSession
    ) -> list[uuid.UUID]:
        """Return run_ids that are actively executing (running/waiting/paused)
        with NO lease row at all — a legacy or orphaned run.

        ``pending`` runs are excluded: they are correctly waiting in the queue
        for a worker, not orphaned mid-execution.
        """
        _actively_executing = ("running", "waiting_approval", "paused")
        runs_result = await session.execute(
            select(AgentRun.id).where(AgentRun.status.in_(_actively_executing))
        )
        run_ids = list(runs_result.scalars().all())
        if not run_ids:
            return []
        # Runs that have a lease.
        lease_result = await session.execute(
            select(RunLease.run_id).where(RunLease.run_id.in_(run_ids))
        )
        leased = set(lease_result.scalars().all())
        return [rid for rid in run_ids if rid not in leased]

    # ------------------------------------------------------------------ #
    async def _reconcile(
        self, run_id: uuid.UUID, lease_id: uuid.UUID | None
    ) -> bool:
        """Requeue or terminally fail one stale run. Returns True if acted."""
        async with self._session_factory() as session:
            run = await session.get(AgentRun, run_id)
            if run is None or run.status not in _NON_TERMINAL:
                return False  # someone already finalized it

            retry_count = await self._count_retries(session, run_id)
            if retry_count < self._max_retries:
                # Requeue: record the retry, give a fresh short lease so the
                # next scan won't immediately re-pick it, and enqueue.
                await EventStore(session).append(
                    run_id,
                    _RECOVERY_EVENT,
                    {"retry": retry_count + 1, "reason": "lease expired"},
                )
                # Clear the old expired lease so the worker can acquire a fresh one.
                if lease_id is not None:
                    old = await session.get(RunLease, lease_id)
                    if old is not None:
                        await session.delete(old)
                run.status = "pending"
                run.error_message = None
                await session.commit()
                await self._queue.requeue(run_id)
                logger.info(
                    "recovery: requeued run %s (retry %d/%d)",
                    run_id, retry_count + 1, self._max_retries,
                )
                return True
            else:
                # Exhausted: terminally fail with an explicit reason.
                run.status = "failed"
                run.finished_at = datetime.now(UTC)
                run.error_message = (
                    "lease expired; recovery exhausted retries "
                    f"({retry_count}/{self._max_retries})"
                )
                # Clean up the stale lease.
                if lease_id is not None:
                    old = await session.get(RunLease, lease_id)
                    if old is not None:
                        await session.delete(old)
                await session.commit()
                logger.warning(
                    "recovery: terminally failed run %s (exhausted %d retries)",
                    run_id, retry_count,
                )
                return True

    async def _count_retries(
        self, session: AsyncSession, run_id: uuid.UUID
    ) -> int:
        """Count prior ``recovery.requeued`` events for this run (the retry counter)."""
        events = await EventStore(session).replay(run_id)
        return sum(1 for e in events if e.event_type == _RECOVERY_EVENT)
