"""Durable run queue: protocol + Redis Streams and InMemory transports (Task 5).

The queue decouples *requesting* a run (``enqueue``) from *executing* it
(``dequeue``). Two transports implement the same contract:

  * :class:`RedisStreamQueue` — production. Uses a Redis Stream + consumer
    group: ``xadd`` to publish, ``xreadgroup`` to claim, ``xack`` to finalize.
    Multiple worker processes share the stream safely.
  * :class:`InMemoryQueue` — deterministic tests / single-worker fallback when
    Redis is unavailable. Mirrors the status-guarded claim pattern of
    :mod:`app.services.background_task_service`.

Both are **idempotent on enqueue**: a run_id that already has a pending entry OR
a live (non-expired) lease is not re-enqueued. This makes duplicate enqueue
calls safe (e.g. the chat handler and a recovery scan racing).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.run_lease import RunLease

logger = logging.getLogger(__name__)


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #
@runtime_checkable
class RunQueue(Protocol):
    """The durable run queue contract."""

    async def enqueue(
        self,
        run_id: uuid.UUID | str,
        *,
        db_session_factory: Callable[[], AsyncSession] | None = None,
    ) -> None:
        """Idempotently enqueue a run. No-op if already pending, in-flight, or
        holding a live lease (when a DB session factory is provided)."""
        ...

    async def pending_ids(self) -> list[uuid.UUID]:
        """Return run_ids that are queued but not yet claimed (for diagnostics)."""
        ...

    async def dequeue(self, owner: str, timeout: float = 0.0) -> uuid.UUID | None:
        """Claim the next pending run_id for ``owner``. Returns ``None`` if the
        queue is empty. ``timeout`` is honored by the Redis transport
        (``xreadgroup block``); InMemoryQueue returns immediately."""
        ...

    async def ack(self, run_id: uuid.UUID | str, owner: str) -> bool:
        """Acknowledge a processed run (remove from in-flight). Returns whether
        the caller was the owning consumer."""
        ...

    async def requeue(self, run_id: uuid.UUID | str) -> None:
        """Re-add a run_id to the pending queue (used by recovery). This
        bypasses the live-lease idempotency check — recovery has already
        determined the lease is expired."""
        ...


# --------------------------------------------------------------------------- #
# In-memory transport (tests / single-worker fallback)
# --------------------------------------------------------------------------- #
class InMemoryQueue:
    """Deterministic in-process queue with FIFO ordering + dedup.

    Mirrors :mod:`app.services.background_task_service`'s status-guarded claim:
    once a run is dequeued (claimed) it moves to an in-flight set and will not
    be returned again until ``ack`` (or ``requeue`` from recovery). An
    :class:`asyncio.Condition` lets a future blocking ``dequeue`` wait, though
    the worker loop currently polls.
    """

    def __init__(self) -> None:
        self._pending: OrderedDict[uuid.UUID, None] = OrderedDict()
        self._in_flight: dict[uuid.UUID, str] = {}  # run_id -> owner
        self._cond = asyncio.Condition()

    async def enqueue(
        self,
        run_id: uuid.UUID | str,
        *,
        db_session_factory: Callable[[], AsyncSession] | None = None,
    ) -> None:
        uid = _as_uuid(run_id)
        async with self._cond:
            if uid in self._pending or uid in self._in_flight:
                return
        # Check for a live lease (DB-backed idempotency).
        if db_session_factory is not None and await _has_live_lease(
            db_session_factory, uid
        ):
            return
        async with self._cond:
            if uid not in self._pending and uid not in self._in_flight:
                self._pending[uid] = None
                self._cond.notify_all()

    async def pending_ids(self) -> list[uuid.UUID]:
        async with self._cond:
            return list(self._pending.keys())

    async def dequeue(self, owner: str, timeout: float = 0.0) -> uuid.UUID | None:
        async with self._cond:
            if not self._pending:
                return None
            uid, _ = self._pending.popitem(last=False)
            self._in_flight[uid] = owner
            return uid

    async def ack(self, run_id: uuid.UUID | str, owner: str) -> bool:
        uid = _as_uuid(run_id)
        async with self._cond:
            if self._in_flight.get(uid) == owner:
                del self._in_flight[uid]
                self._cond.notify_all()
                return True
            return False

    async def requeue(self, run_id: uuid.UUID | str) -> None:
        uid = _as_uuid(run_id)
        async with self._cond:
            # Clear any stale in-flight entry from the dead worker.
            self._in_flight.pop(uid, None)
            if uid not in self._pending:
                self._pending[uid] = None
                self._cond.notify_all()


# --------------------------------------------------------------------------- #
# Redis Streams transport (production)
# --------------------------------------------------------------------------- #
class RedisStreamQueue:
    """Redis Streams-backed queue using a consumer group.

    * ``enqueue`` → ``XADD`` with the run_id as a field; deduped by checking the
      group's pending entries list (PEL) and a short recent-id window.
    * ``dequeue`` → ``XREADGROUP GROUP <group> <owner>`` claiming new messages.
    * ``ack`` → ``XACK``.
    * ``requeue`` → ``XADD`` (recovery path; the old entry is left for audit).

    Never raises on Redis errors: callers handle None return from dequeue.
    """

    def __init__(
        self,
        client: Any,
        stream: str | None = None,
        group: str | None = None,
    ) -> None:
        self._client = client
        settings = get_settings()
        self._stream = stream or settings.RUN_QUEUE_STREAM
        self._group = group or settings.RUN_QUEUE_GROUP
        self._initialized = False

    async def _ensure_group(self) -> None:
        """Create the consumer group (idempotent; ignores BUSYGROUP)."""
        if self._initialized:
            return
        try:
            await self._client.xgroup_create(self._stream, self._group, id="0", mkstream=True)
        except Exception as exc:  # noqa: BLE001
            if "BUSYGROUP" not in str(exc):
                logger.debug("xgroup_create %s: %s", self._stream, exc)
        self._initialized = True

    async def enqueue(
        self,
        run_id: uuid.UUID | str,
        *,
        db_session_factory: Callable[[], AsyncSession] | None = None,
    ) -> None:
        uid = _as_uuid(run_id)
        # Live-lease idempotency (DB-backed).
        if db_session_factory is not None and await _has_live_lease(
            db_session_factory, uid
        ):
            return
        await self._ensure_group()
        try:
            # Check PEL for an unacked entry for this run_id.
            if await self._has_pending(uid):
                return
            await self._client.xadd(
                self._stream, {"run_id": str(uid)},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("enqueue %s failed: %s", uid, exc)

    async def _has_pending(self, uid: uuid.UUID) -> bool:
        """True if the run_id has an unacked entry in the consumer group."""
        try:
            # XPENDING returns summary; we check the PEL entries.
            info = await self._client.xpending_range(
                self._stream, self._group, min="-", max="+", count=1000
            )
            for entry in info:
                msg_id = entry.get("message_id") if isinstance(entry, dict) else None
                if msg_id is None:
                    continue
                fields = await self._client.xrange(self._stream, msg_id, msg_id, count=1)
                for _mid, data in fields:
                    if data.get("run_id") == str(uid):
                        return True
        except Exception:  # noqa: BLE001
            pass
        return False

    async def pending_ids(self) -> list[uuid.UUID]:
        await self._ensure_group()
        try:
            info = await self._client.xpending_range(
                self._stream, self._group, min="-", max="+", count=1000
            )
            result = []
            for entry in info:
                msg_id = entry.get("message_id") if isinstance(entry, dict) else None
                if msg_id is None:
                    continue
                fields = await self._client.xrange(self._stream, msg_id, msg_id, count=1)
                for _mid, data in fields:
                    rid = data.get("run_id")
                    if rid:
                        try:
                            result.append(uuid.UUID(rid))
                        except (ValueError, TypeError):
                            pass
            return result
        except Exception:  # noqa: BLE001
            return []

    async def dequeue(self, owner: str, timeout: float = 0.0) -> uuid.UUID | None:
        await self._ensure_group()
        block_ms = int(timeout * 1000) if timeout > 0 else None
        try:
            resp = await self._client.xreadgroup(
                self._group,
                owner,
                {self._stream: ">"},
                count=1,
                block=block_ms,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("xreadgroup failed: %s", exc)
            return None
        if not resp:
            return None
        for _stream, messages in resp:
            for _mid, data in messages:
                rid = data.get("run_id")
                if rid:
                    try:
                        return uuid.UUID(rid)
                    except (ValueError, TypeError):
                        pass
        return None

    async def ack(self, run_id: uuid.UUID | str, owner: str) -> bool:
        """Ack all PEL entries whose run_id field matches."""
        uid = _as_uuid(run_id)
        await self._ensure_group()
        try:
            info = await self._client.xpending_range(
                self._stream, self._group, min="-", max="+", count=1000
            )
            acked = False
            for entry in info:
                msg_id = entry.get("message_id") if isinstance(entry, dict) else None
                if msg_id is None:
                    continue
                fields = await self._client.xrange(self._stream, msg_id, msg_id, count=1)
                for _mid, data in fields:
                    if data.get("run_id") == str(uid):
                        await self._client.xack(self._stream, self._group, msg_id)
                        acked = True
            return acked
        except Exception as exc:  # noqa: BLE001
            logger.warning("ack %s failed: %s", uid, exc)
            return False

    async def requeue(self, run_id: uuid.UUID | str) -> None:
        uid = _as_uuid(run_id)
        try:
            await self._client.xadd(self._stream, {"run_id": str(uid)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("requeue %s failed: %s", uid, exc)


# --------------------------------------------------------------------------- #
# Lease idempotency helper
# --------------------------------------------------------------------------- #
async def _has_live_lease(
    session_factory: Callable[[], AsyncSession], run_id: uuid.UUID
) -> bool:
    """True if the run has a non-expired lease (DB-backed idempotency check)."""
    try:
        async with session_factory() as session:
            result = await session.execute(
                select(RunLease).where(RunLease.run_id == run_id)
            )
            lease = result.scalar_one_or_none()
            if lease is None:
                return False
            now = datetime.now(timezone.utc)
            # expires_at may be naive (SQLite) or aware (PG); normalise.
            exp = lease.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return now < exp
    except Exception:  # noqa: BLE001 — never crash on lease check
        return False


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
_queue_singleton: RunQueue | None = None


async def get_run_queue() -> RunQueue:
    """Return the process-wide run queue.

    Returns :class:`RedisStreamQueue` when Redis is reachable AND
    ``BACKGROUND_WORKER != "inprocess"``; otherwise degrades to
    :class:`InMemoryQueue` (single-worker) like :mod:`app.agents.approval_bus`.
    Never crashes: a Redis outage falls back silently.
    """
    global _queue_singleton
    if _queue_singleton is not None:
        return _queue_singleton

    settings = get_settings()
    if settings.BACKGROUND_WORKER == "inprocess":
        _queue_singleton = InMemoryQueue()
        return _queue_singleton

    try:
        from app.core.redis import get_redis
        client = get_redis()
        await client.ping()
        _queue_singleton = RedisStreamQueue(client)
        logger.info("run queue: Redis Streams transport (stream=%s)", settings.RUN_QUEUE_STREAM)
    except Exception as exc:  # noqa: BLE001
        logger.warning("run queue: Redis unavailable (%s), falling back to InMemoryQueue", exc)
        _queue_singleton = InMemoryQueue()

    return _queue_singleton


def set_run_queue(queue: RunQueue | None) -> None:
    """Inject a queue (testing / forced override). Pass ``None`` to reset."""
    global _queue_singleton
    _queue_singleton = queue
