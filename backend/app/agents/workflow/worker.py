"""Durable run worker loop (Task 5).

The worker claims a run from the queue, acquires a lease, executes the run
(persisting each emitted :class:`~app.agents.schemas.AgentEvent` as a durable
:class:`~app.models.RunEvent`), and finalizes: release lease + ack on success,
requeue on transient failure. A background task renews the lease periodically
so a long run doesn't lose ownership.

The execution function is **injected** (``execute_fn``) so the worker mechanics
are fully testable without the real runtime. The production executor
(:func:`execute_run`) reconstructs the turn context and runs the orchestrator.

Design:
  * Each run executes on its OWN session (never reused across runs/tasks).
  * Durable event appends use a FRESH short-lived session (not ``exec_session``)
    so a runtime intermediate ORM write can never be flushed half-formed — the
    ``exec_session`` stays the runtime's sole property.
  * The lease renewal loop runs as a background task, stopped on finalize.
    If the lease is LOST (expired + taken over by another worker) the renewal
    loop signals the execution loop via a shared ``asyncio.Event``; the
    execution loop stops mutating the run and exits WITHOUT acking, leaving
    recovery to requeue the run. This prevents split-brain execution.
  * Terminal events (``run.completed`` / ``run.failed``) close the run and ack.
  * A transient exception during execution requeues the run for retry (bounded
    by :class:`~app.agents.workflow.recovery.RecoveryScheduler`).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, UTC
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.events import append_event_safe
from app.agents.schemas import AgentEvent
from app.agents.workflow.queue import RunQueue
from app.agents.workflow.repository import LeaseStore
from app.core.config import get_settings
from app.models.agent_run import AgentRun

logger = logging.getLogger(__name__)

# Event types that signal the run has reached a terminal state.
_TERMINAL_EVENT_TYPES = frozenset(
    {"run.completed", "run.failed", "run.cancelled", "done", "error"}
)

#: Execution seam: yields AgentEvents for a run. The worker persists each one.
ExecuteFn = Callable[[uuid.UUID, AsyncSession], "AsyncIterator[AgentEvent]"]


class RunWorker:
    """Claim → lease → execute → finalize one run at a time."""

    def __init__(
        self,
        queue: RunQueue,
        execute_fn: ExecuteFn,
        *,
        session_factory: Callable[[], AsyncSession] | None = None,
        owner: str | None = None,
        settings: Any = None,
    ) -> None:
        self._queue = queue
        self._execute_fn = execute_fn
        self._session_factory = session_factory or _default_session_factory()
        self._owner = owner or f"worker-{uuid.uuid4().hex[:8]}"
        self._settings = settings or get_settings()
        self._ttl = self._settings.RUN_LEASE_TTL_SECONDS
        self._renew_interval = self._settings.RUN_LEASE_RENEW_SECONDS
        self._poll_interval = self._settings.WORKER_POLL_INTERVAL_SECONDS
        self._block_timeout = self._settings.WORKER_BLOCK_TIMEOUT_SECONDS
        # Event batching knobs (see _process): flush on N events or T seconds.
        self._event_batch_size = max(int(getattr(self._settings, "RUN_EVENT_BATCH_SIZE", 32) or 32), 1)
        self._event_flush_interval = max(
            float(getattr(self._settings, "RUN_EVENT_FLUSH_SECONDS", 0.2) or 0.2), 0.02
        )

    async def run_once(self) -> uuid.UUID | None:
        """Claim and process one run. Returns the run_id, or None if idle."""
        run_id = await self._queue.dequeue(self._owner, timeout=self._block_timeout)
        if run_id is None:
            return None
        try:
            await self._process(run_id)
        except Exception:
            logger.exception("worker %s: unhandled error processing %s", self._owner, run_id)
        return run_id

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        """Claim → process loop until ``stop_event`` is set (graceful shutdown)."""
        if stop_event is None:
            stop_event = asyncio.Event()
        while not stop_event.is_set():
            processed = await self.run_once()
            if processed is None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval)
                except TimeoutError:
                    pass

    # ------------------------------------------------------------------ #
    async def _process(self, run_id: uuid.UUID) -> None:
        """Acquire lease, execute, finalize. Handles transient failures.

        On lease loss mid-execution (the renewal loop detected another owner
        took over), the worker stops mutating the run and exits WITHOUT
        acking — recovery owns the requeue. This prevents two workers from
        executing the same run concurrently (split-brain).
        """
        # 1. Acquire lease (take over from any expired one).
        async with self._session_factory() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                await self._queue.ack(run_id, self._owner)
                return
            # Skip already-terminal runs (e.g. cancelled while queued).
            if run.status in ("completed", "failed", "cancelled"):
                await self._queue.ack(run_id, self._owner)
                return
            await LeaseStore(session).acquire(run_id, self._owner, self._ttl)
            run.status = "running"
            run.started_at = run.started_at or datetime.now(UTC)
            await append_event_safe(session, run_id, "run.started", {
                "run_id": str(run_id),
                "owner": self._owner,
            })
            await session.commit()

        # 2. Execute with lease renewal.
        renewal_stop = asyncio.Event()
        lease_lost = asyncio.Event()
        renewal_task = asyncio.create_task(
            self._renewal_loop(run_id, renewal_stop, lease_lost)
        )
        terminal = False
        lease_aborted = False
        # Event batching: buffer streamed events and flush in batches (bounded
        # by count and time). Persisting one session+commit PER TOKEN DELTA was
        # a 3-orders-of-magnitude write amplification (a 2000-token reply =
        # 2000+ rows, 6000+ round trips) that pinned the DB under load. The
        # SSE replay path polls at ~150ms anyway, so a 200ms flush cadence is
        # invisible to clients.
        batch: list[tuple[str, dict]] = []
        last_flush = time.monotonic()
        try:
            async with self._session_factory() as exec_session:
                async for evt in self._execute_fn(run_id, exec_session):
                    # Lease-loss fence: stop persisting + finalizing the moment
                    # the renewal loop detects we lost ownership. Recovery will
                    # requeue the run for another worker.
                    if lease_lost.is_set():
                        logger.warning(
                            "worker %s: aborting run %s after lease loss "
                            "(recovery will requeue)", self._owner, run_id,
                        )
                        lease_aborted = True
                        # Drop (don't flush) the buffer: the new lease holder
                        # re-emits these events; interleaving two writers'
                        # batches could corrupt sequence order.
                        batch.clear()
                        break
                    batch.append((evt.kind, evt.data))
                    due = (
                        len(batch) >= self._event_batch_size
                        or time.monotonic() - last_flush >= self._event_flush_interval
                    )
                    if evt.kind in _TERMINAL_EVENT_TYPES or due:
                        await self._persist_event_batch(run_id, batch)
                        batch.clear()
                        last_flush = time.monotonic()
                    if evt.kind in _TERMINAL_EVENT_TYPES:
                        terminal = True
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Flush whatever completed before the failure so replay/SSE keeps
            # the partial history, then release the lease WITHOUT acking and
            # WITHOUT marking the run failed: recovery requeues it under its
            # retry budget (RUN_MAX_RETRIES). Failing the run on the FIRST
            # transient exception (one DB blip = a user-visible failed reply)
            # contradicted the designed recovery path this worker ships with.
            logger.warning(
                "worker %s: run %s failed transiently, abandoning to recovery: %s",
                self._owner, run_id, exc,
            )
            try:
                if batch:
                    await self._persist_event_batch(run_id, batch)
                    batch.clear()
            except Exception:
                pass
            await self._abandon_to_recovery(run_id, str(exc)[:500])
            return
        finally:
            renewal_stop.set()
            if not renewal_task.done():
                renewal_task.cancel()
                try:
                    await renewal_task
                except (asyncio.CancelledError, Exception):
                    pass

        # 3. Finalize.
        if lease_aborted:
            # Lease lost: do NOT finalize or ack. The run stays in-flight in the
            # queue; recovery detects the expired/stolen lease and requeues it.
            logger.info(
                "worker %s: leaving run %s for recovery after lease loss",
                self._owner, run_id,
            )
            return
        # Generator exhausted (with or without a terminal event): flush any
        # residual buffered events before success finalization.
        if batch:
            await self._persist_event_batch(run_id, batch)
            batch.clear()
        await self._finalize_success(run_id, terminal)

    # ------------------------------------------------------------------ #
    async def _persist_event(self, run_id: uuid.UUID, evt: AgentEvent) -> None:
        """Persist one AgentEvent as a durable RunEvent on a SHORT-LIVED session.

        Using a fresh session (instead of ``exec_session``) isolates the durable
        event append from the runtime's ORM state on ``exec_session`` — a
        runtime intermediate write can never be flushed half-formed by the
        event-append commit. Best-effort: a failure is logged, not raised.

        The streaming path prefers :meth:`_persist_event_batch` (one commit per
        batch instead of per token delta).
        """
        try:
            async with self._session_factory() as session:
                await append_event_safe(session, run_id, evt.kind, evt.data)
                await session.commit()
        except Exception:
            logger.debug(
                "worker %s: durable event append failed (%s for run %s)",
                self._owner, evt.kind, run_id, exc_info=True,
            )

    async def _persist_event_batch(
        self, run_id: uuid.UUID, batch: list[tuple[str, dict]]
    ) -> None:
        """Persist a batch of buffered events in ONE session + commit.

        One ``max(sequence)`` read covers the whole batch (EventStore
        :meth:`~app.agents.events.EventStore.append_many`), so per-token
        persistence drops from O(tokens) transactions to O(batches). The
        short-lived session keeps the same isolation as ``_persist_event``.
        Best-effort: a failure is logged and the batch is dropped (the run's
        outcome rows/status carry the authoritative state).
        """
        if not batch:
            return
        try:
            async with self._session_factory() as session:
                from app.agents.events import EventStore

                await EventStore(session).append_many(run_id, batch)
                await session.commit()
        except Exception:
            logger.debug(
                "worker %s: durable event batch append failed (%d events for run %s)",
                self._owner, len(batch), run_id, exc_info=True,
            )

    async def _abandon_to_recovery(self, run_id: uuid.UUID, error: str) -> None:
        """Release the lease on an execution failure WITHOUT failing the run.

        Recovery's bounded retry budget (RUN_MAX_RETRIES) then owns the run:
        the expired lease is detected, the run is requeued, and only after the
        budget is exhausted is it marked failed. (The previous behaviour
        ack'd + marked failed on the FIRST exception, so a single transient DB
        blip surfaced to the user as a failed reply.)
        """
        try:
            async with self._session_factory() as session:
                await LeaseStore(session).release(run_id, self._owner)
                await session.commit()
        except Exception:
            logger.debug(
                "worker %s: lease release during abandon failed for run %s",
                self._owner, run_id, exc_info=True,
            )

    async def _renewal_loop(
        self, run_id: uuid.UUID, stop: asyncio.Event, lease_lost: asyncio.Event
    ) -> None:
        """Periodically renew the lease so a long run keeps ownership.

        On lease loss (``renew`` returns ``None`` — another owner took over
        after TTL expiry), signals ``lease_lost`` so :meth:`_process` can abort
        the execution loop instead of continuing to mutate the run.
        """
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._renew_interval)
                except TimeoutError:
                    pass
                if stop.is_set():
                    return
                async with self._session_factory() as session:
                    renewed = await LeaseStore(session).renew(
                        run_id, self._owner, self._ttl
                    )
                    if renewed is None:
                        # Lost the lease (expired and taken over). Signal the
                        # execution loop to stop; do NOT touch the run further.
                        logger.warning(
                            "worker %s: lost lease for run %s", self._owner, run_id
                        )
                        lease_lost.set()
                        return
                    await session.commit()
        except asyncio.CancelledError:
            raise

    async def _finalize_success(self, run_id: uuid.UUID, terminal: bool) -> None:
        """Release the lease and ack the queue on a clean completion."""
        async with self._session_factory() as session:
            await LeaseStore(session).release(run_id, self._owner)
            if terminal:
                run = await session.get(AgentRun, run_id)
                if run is not None and run.status not in ("completed", "failed", "cancelled"):
                    run.status = "completed"
                    run.finished_at = datetime.now(UTC)
            await session.commit()
        await self._queue.ack(run_id, self._owner)

    async def _finalize_failure(self, run_id: uuid.UUID, error: str) -> None:
        """Release the lease, mark the run failed, and ack (no requeue from here;
        recovery handles bounded retries via the event-counted retry budget)."""
        async with self._session_factory() as session:
            await LeaseStore(session).release(run_id, self._owner)
            run = await session.get(AgentRun, run_id)
            if run is not None and run.status not in ("completed", "failed", "cancelled"):
                run.status = "failed"
                run.finished_at = datetime.now(UTC)
                run.error_message = error
            await session.commit()
        await self._queue.ack(run_id, self._owner)


def _default_session_factory() -> Callable[[], AsyncSession]:
    """Return the app's AsyncSessionLocal (lazy import so the module loads in tests)."""
    from app.db import AsyncSessionLocal
    return AsyncSessionLocal
