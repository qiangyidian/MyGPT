"""Durable run worker loop (Task 5).

The worker claims a run from the queue, acquires a lease, executes the run
(persisting each emitted :class:`~app.agents.schemas.AgentEvent` as a durable
:class:`~app.models.RunEvent`), and finalizes: release lease + ack on success,
requeue on transient failure. A background task renews the lease periodically
so a long run doesn't lose ownership.

The execution function is **injected** (``execute_fn``) so the worker mechanics
are fully testable without the real runtime. The production executor
(:func:`execute_run`) reconstructs the turn context and runs the orchestrator;
see its docstring for the deferred wiring note.

Design:
  * Each run executes on its OWN session (never reused across runs/tasks).
  * The lease renewal loop runs as a background task, stopped on finalize.
  * Terminal events (``run.completed`` / ``run.failed``) close the run and ack.
  * A transient exception during execution requeues the run for retry (bounded
    by :class:`~app.agents.workflow.recovery.RecoveryScheduler`).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable

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

    async def run_once(self) -> uuid.UUID | None:
        """Claim and process one run. Returns the run_id, or None if idle."""
        run_id = await self._queue.dequeue(self._owner, timeout=self._block_timeout)
        if run_id is None:
            return None
        try:
            await self._process(run_id)
        except Exception:  # noqa: BLE001 — never crash the worker
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
                except asyncio.TimeoutError:
                    pass

    # ------------------------------------------------------------------ #
    async def _process(self, run_id: uuid.UUID) -> None:
        """Acquire lease, execute, finalize. Handles transient failures."""
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
            run.started_at = run.started_at or datetime.now(timezone.utc)
            await append_event_safe(session, run_id, "run.started", {
                "run_id": str(run_id),
                "owner": self._owner,
            })
            await session.commit()

        # 2. Execute with lease renewal.
        renewal_stop = asyncio.Event()
        renewal_task = asyncio.create_task(
            self._renewal_loop(run_id, renewal_stop)
        )
        terminal = False
        try:
            async with self._session_factory() as exec_session:
                async for evt in self._execute_fn(run_id, exec_session):
                    await _persist_event(exec_session, run_id, evt)
                    if evt.kind in _TERMINAL_EVENT_TYPES:
                        terminal = True
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — transient: requeue
            logger.warning("worker %s: run %s failed transiently: %s", self._owner, run_id, exc)
            await self._finalize_failure(run_id, str(exc)[:500])
            return
        finally:
            renewal_stop.set()
            if not renewal_task.done():
                renewal_task.cancel()
                try:
                    await renewal_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        # 3. Finalize: release lease + ack.
        await self._finalize_success(run_id, terminal)

    # ------------------------------------------------------------------ #
    async def _renewal_loop(self, run_id: uuid.UUID, stop: asyncio.Event) -> None:
        """Periodically renew the lease so a long run keeps ownership."""
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._renew_interval)
                except asyncio.TimeoutError:
                    pass
                if stop.is_set():
                    return
                async with self._session_factory() as session:
                    renewed = await LeaseStore(session).renew(
                        run_id, self._owner, self._ttl
                    )
                    if renewed is None:
                        # Lost the lease (expired and taken over). Stop executing.
                        logger.warning(
                            "worker %s: lost lease for run %s", self._owner, run_id
                        )
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
                    run.finished_at = datetime.now(timezone.utc)
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
                run.finished_at = datetime.now(timezone.utc)
                run.error_message = error
            await session.commit()
        await self._queue.ack(run_id, self._owner)


async def _persist_event(session: AsyncSession, run_id: uuid.UUID, evt: AgentEvent) -> None:
    """Persist one AgentEvent as a durable RunEvent (best-effort)."""
    await append_event_safe(session, run_id, evt.kind, evt.data)
    await session.commit()


def _default_session_factory() -> Callable[[], AsyncSession]:
    """Return the app's AsyncSessionLocal (lazy import so the module loads in tests)."""
    from app.db import AsyncSessionLocal
    return AsyncSessionLocal
