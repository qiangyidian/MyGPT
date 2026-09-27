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
from collections.abc import Callable
from datetime import datetime, UTC
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.run_lease import RunLease
from app.observability import observe_counter, observe_histogram

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

    async def reclaim_stale(self, owner: str, *, limit: int = 100) -> int:
        """Requeue entries stuck in this group's pending list with no live
        consumer. Returns how many were requeued.

        Only the Redis transport has anything to reclaim; every other
        transport's PEL is the process itself, so a no-op (0) is the honest
        answer. Callers must gate this behind leader election — concurrent
        reclaimers would each claim and requeue the same entry.
        """
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
        # value = enqueue timestamp (monotonic) so dequeue can observe wait time.
        self._pending: OrderedDict[uuid.UUID, float] = OrderedDict()
        self._in_flight: dict[uuid.UUID, str] = {}  # run_id -> owner
        self._cond = asyncio.Condition()

    async def enqueue(
        self,
        run_id: uuid.UUID | str,
        *,
        db_session_factory: Callable[[], AsyncSession] | None = None,
    ) -> None:
        import time as _time

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
                self._pending[uid] = _time.monotonic()
                self._cond.notify_all()
        observe_counter("queue.enqueues", 1)

    async def pending_ids(self) -> list[uuid.UUID]:
        async with self._cond:
            return list(self._pending.keys())

    async def dequeue(self, owner: str, timeout: float = 0.0) -> uuid.UUID | None:
        import time as _time

        async with self._cond:
            if not self._pending:
                return None
            uid, enqueued_at = self._pending.popitem(last=False)
            self._in_flight[uid] = owner
        # Observability (Task 11b): record the enqueue→dequeue wait time. Inert
        # when exporters are off; the test recorder captures it regardless.
        observe_histogram(
            "queue.wait_ms", int((_time.monotonic() - enqueued_at) * 1000),
        )
        observe_counter("queue.dequeues", 1)
        return uid

    async def ack(self, run_id: uuid.UUID | str, owner: str) -> bool:
        uid = _as_uuid(run_id)
        async with self._cond:
            if self._in_flight.get(uid) == owner:
                del self._in_flight[uid]
                self._cond.notify_all()
                observe_counter("queue.acks", 1, outcome="owned")
                return True
            observe_counter("queue.acks", 1, outcome="not_owner")
            return False

    async def requeue(self, run_id: uuid.UUID | str) -> None:
        import time as _time

        uid = _as_uuid(run_id)
        async with self._cond:
            # Clear any stale in-flight entry from the dead worker.
            self._in_flight.pop(uid, None)
            if uid not in self._pending:
                self._pending[uid] = _time.monotonic()
                self._cond.notify_all()

    async def reclaim_stale(self, owner: str, *, limit: int = 100) -> int:
        """No pending list to reclaim: the in-memory queue dies with its process,
        and everything it held is re-derived from the DB by recovery on boot."""
        return 0


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
        # XADD cap (approximate, i.e. the ``~`` form) — every run leaves a stream
        # entry, so without this the stream grows for the lifetime of the Redis
        # instance. Approximate trimming only cuts whole nodes, which is what
        # keeps it cheap; entries still inside the consumer group's PEL are NOT
        # freed by trimming, so an unacked run is never silently lost (its
        # reclaim path below reads the PEL, not the stream body).
        self._maxlen = max(int(settings.RUN_STREAM_MAXLEN), 1000)
        self._claim_idle_ms = max(int(settings.RUN_STREAM_CLAIM_IDLE_SECONDS), 1) * 1000
        # PEL entries a single scan reads; see RUN_STREAM_PENDING_SCAN in config
        # for why truncating this is a correctness problem, not a slow one.
        self._pending_scan = max(int(settings.RUN_STREAM_PENDING_SCAN), 1)
        self._initialized = False

    async def _ensure_group(self) -> None:
        """Create the consumer group (idempotent; ignores BUSYGROUP)."""
        if self._initialized:
            return
        try:
            await self._client.xgroup_create(self._stream, self._group, id="0", mkstream=True)
        except Exception as exc:
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
                maxlen=self._maxlen, approximate=True,
            )
        except Exception as exc:
            logger.warning("enqueue %s failed: %s", uid, exc)

    async def _pending_entries(self) -> list[tuple[str, str]]:
        """``(message_id, run_id)`` for entries still unacked in our group.

        Two round trips no matter how deep the backlog: one ``XPENDING`` for the
        ids, one ``XRANGE`` spanning them. The previous shape issued an
        ``XRANGE`` **per pending entry** from three places, so ``enqueue`` — the
        hottest path in the worker, run for every submission — paid a full
        walk of the pending set (N network round trips) just to ask "is this run
        already queued?", and ``ack`` did it again on completion.

        An entry whose body ``maxlen`` already trimmed away yields no ``run_id``
        and is left out: there is nothing to match or ack from a body we cannot
        read, and :meth:`reclaim_stale` handles those explicitly (it acks a
        deleted claim rather than reviving it).
        """
        try:
            info = await self._client.xpending_range(
                self._stream,
                self._group,
                min="-",
                max="+",
                count=self._pending_scan,
            )
        except Exception as exc:
            logger.debug("xpending %s: %s", self._stream, exc)
            return []
        # XPENDING returns PEL entries in ascending id order, so the first and
        # last ids bound the span the single XRANGE has to cover.
        pending: list[str] = []
        for entry in info or []:
            msg_id = entry.get("message_id") if isinstance(entry, dict) else None
            if msg_id is not None:
                pending.append(str(msg_id))
        if not pending:
            return []
        try:
            rows = await self._client.xrange(
                self._stream, min=pending[0], max=pending[-1]
            )
        except Exception as exc:
            logger.debug("xrange %s: %s", self._stream, exc)
            return []
        want = set(pending)
        found: list[tuple[str, str]] = []
        for msg_id, data in rows or []:
            mid = str(msg_id)
            if mid not in want:
                # Inside the span but not pending (already acked, or a newer
                # entry) — acking it would hand another consumer's in-flight
                # run back to the queue.
                continue
            rid = (data or {}).get("run_id")
            if rid:
                found.append((mid, str(rid)))
        return found

    async def _has_pending(self, uid: uuid.UUID) -> bool:
        """True if the run_id has an unacked entry in the consumer group."""
        target = str(uid)
        return any(rid == target for _mid, rid in await self._pending_entries())

    async def pending_ids(self) -> list[uuid.UUID]:
        await self._ensure_group()
        result: list[uuid.UUID] = []
        for _mid, rid in await self._pending_entries():
            try:
                result.append(uuid.UUID(rid))
            except (ValueError, TypeError):
                pass
        return result

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
        except Exception as exc:
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
        target = str(uid)
        # One scan, then a single XACK: the old loop acked each match with its
        # own round trip *and* re-read the PEL per entry, so finishing a run
        # cost as much traffic as the whole backlog.
        matching = [
            mid for mid, rid in await self._pending_entries() if rid == target
        ]
        if not matching:
            return False
        try:
            await self._client.xack(self._stream, self._group, *matching)
        except Exception as exc:
            logger.warning("ack %s failed: %s", uid, exc)
            return False
        return True

    async def requeue(self, run_id: uuid.UUID | str) -> None:
        uid = _as_uuid(run_id)
        try:
            await self._client.xadd(
                self._stream, {"run_id": str(uid)},
                maxlen=self._maxlen, approximate=True,
            )
        except Exception as exc:
            logger.warning("requeue %s failed: %s", uid, exc)

    async def reclaim_stale(self, owner: str, *, limit: int = 100) -> int:
        """Requeue PEL entries no consumer has touched in ``_claim_idle_ms``.

        This is the crash window the lease/recovery path cannot see: a worker
        that dies *after* ``XREADGROUP`` delivered a message but *before* it
        wrote a lease row leaves an entry claimed to a consumer that no longer
        exists. Nothing re-reads it — ``XREADGROUP`` only hands out new entries
        via ``>``, and recovery scans the database, not the stream. The run sits
        pending forever.

        ``XAUTOCLAIM`` moves those entries to ``owner`` (which also resets their
        idle clock and delivers them exactly once to this caller), then we
        ``XADD`` a fresh entry so any live worker picks it up and ``XACK`` the
        claimed id. The old entry stays in the stream for audit, matching
        :meth:`requeue`.

        Returns the number of entries requeued. Only ever called from a
        leader-gated loop (see :mod:`app.core.leader`), so two processes cannot
        race the same claim; a duplicate requeue would anyway be harmless
        because ``execute_run`` re-checks the run's terminal status under lease.
        """
        await self._ensure_group()
        claimed: list[Any] = []
        try:
            resp = await self._client.xautoclaim(
                self._stream, self._group, owner,
                min_idle_time=self._claim_idle_ms, start_id="0-0", count=limit,
            )
            # redis-py returns (next-cursor, messages[, deleted-ids]) — the
            # DELETED variant on Redis >= 7). Accept the shapes that matter.
            if isinstance(resp, (list, tuple)) and len(resp) >= 2:
                claimed = resp[1] or []
        except Exception as exc:
            # Older servers (< 6.2) have no XAUTOCLAIM — log once per call, the
            # lease-based recovery scheduler still covers every other crash path.
            logger.debug("xautoclaim %s: %s", self._stream, exc)
            return 0
        requeued = 0
        for item in claimed:
            msg_id, data = self._claim_entry(item)
            if msg_id is None:
                continue
            rid = (data or {}).get("run_id")
            if not rid:
                # A claim whose body was already trimmed carries no run to
                # revive; ack it so it stops showing up in XPENDING.
                await self._drop_claim(msg_id, owner)
                continue
            try:
                await self._client.xadd(
                    self._stream, {"run_id": str(rid)},
                    maxlen=self._maxlen, approximate=True,
                )
                await self._client.xack(self._stream, self._group, msg_id)
                requeued += 1
                logger.info("reclaimed stalled run %s (stream id %s)", rid, msg_id)
            except Exception as exc:
                logger.warning("reclaim %s failed: %s", rid, exc)
        return requeued

    @staticmethod
    def _claim_entry(item: Any) -> tuple[str | None, dict[str, Any] | None]:
        """Normalise one XAUTOCLAIM item to ``(message_id, fields)``.

        redis-py hands back a flat ``[id, {fields}, id, {fields}, ...]`` list on
        some versions and ``[(id, {fields}), ...]`` on others; a claim with a
        deleted body comes back as a bare id. Both shapes plus the bare id are
        handled so the reclaim never silently no-ops on a client upgrade.
        """
        if isinstance(item, (list, tuple)):
            if len(item) == 2 and isinstance(item[1], dict):
                return str(item[0]), item[1]
            if len(item) == 1:
                return str(item[0]), None
            return None, None
        if isinstance(item, dict):
            mid = item.get("message_id") or item.get("id")
            return (str(mid), item) if mid else (None, None)
        return (str(item), None) if item else (None, None)

    async def _drop_claim(self, msg_id: str, owner: str) -> None:
        """Ack a claim with no recoverable body (trimmed entry)."""
        try:
            await self._client.xack(self._stream, self._group, msg_id)
        except Exception as exc:
            logger.debug("ack of trimmed claim %s failed: %s", msg_id, exc)

    async def stream_depth(self) -> tuple[int, int]:
        """Return ``(stream_length, pending_entries)`` for this transport.

        The two numbers answer different operational questions and are alerted
        on separately: a large *length* with a small *pending* count means work
        is arriving faster than it drains (capacity), while a growing *pending*
        count means entries were delivered and never acked (workers are dying or
        wedged). Reading both from the API process is what lets ``/metrics``
        expose queue lag at all — the worker has no scrape surface, so a metric
        only it observed would be invisible to Alertmanager.
        """
        try:
            length = int(await self._client.xlen(self._stream))
        except Exception:
            length = 0
        pending = 0
        try:
            await self._ensure_group()
            summary = await self._client.xpending(self._stream, self._group)
            if isinstance(summary, dict):
                pending = int(summary.get("pending") or 0)
            elif isinstance(summary, (list, tuple)) and summary:
                pending = int(summary[0] or 0)
        except Exception as exc:
            logger.debug("xpending %s: %s", self._stream, exc)
        return length, pending


# --------------------------------------------------------------------------- #
# Queue-depth gauge poller (metrics for Alertmanager)
# --------------------------------------------------------------------------- #
async def run_queue_depth_poller(
    stop: asyncio.Event | None = None, *, interval: float = 15.0
) -> None:
    """Publish ``queue.stream_length`` / ``queue.pending_entries`` as gauges.

    Started from the API lifespan only when ``PROMETHEUS_ENABLED``; exits on
    ``stop`` or task cancellation. Best-effort by design: a Redis blip must not
    take the scrape down with it, so a failed sample is skipped (the previous
    value ages out and ``absent_over_time``/staleness handles it) rather than
    exported as a misleading zero.
    """
    from app.observability import observe_gauge

    poll = max(float(interval), 1.0)
    while stop is None or not stop.is_set():
        try:
            queue = await get_run_queue()
            depth = getattr(queue, "stream_depth", None)
            if depth is not None:
                length, pending = await depth()
                observe_gauge("queue.stream_length", float(length))
                observe_gauge("queue.pending_entries", float(pending))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("queue depth poll failed", exc_info=True)
        if stop is None:
            await asyncio.sleep(poll)
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll)
        except TimeoutError:
            pass


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
            now = datetime.now(UTC)
            # expires_at may be naive (SQLite) or aware (PG); normalise.
            exp = lease.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            return now < exp
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
_queue_singleton: RunQueue | None = None


class RunQueueUnavailable(RuntimeError):
    """Durable mode requires Redis but it is unreachable.

    Deliberately NOT a silent fallback: in durable mode the API process only
    *enqueues* while a separate worker process *consumes*. Caching an
    in-memory queue on a transient Redis outage stranded every newly created
    run in a queue no worker could ever see — runs stayed ``pending`` forever
    and the user's SSE hung. Failing fast leaves the run row persisted as
    ``pending`` (the recovery scheduler re-enqueues it once Redis is back).
    """


async def get_run_queue(*, retries: int = 3, retry_delay: float = 0.5) -> RunQueue:
    """Return the process-wide run queue.

    ``BACKGROUND_WORKER == "inprocess"`` → :class:`InMemoryQueue` (runs execute
    inside the API process; a memory queue is correct there).

    Durable mode → :class:`RedisStreamQueue` is REQUIRED (it is the transport
    between the API and the separate worker). A short retry window absorbs
    transient blips; after that :class:`RunQueueUnavailable` is raised so
    callers fail fast / the worker process exits and systemd restarts it.
    """
    global _queue_singleton
    if _queue_singleton is not None:
        return _queue_singleton

    settings = get_settings()
    if settings.BACKGROUND_WORKER == "inprocess":
        _queue_singleton = InMemoryQueue()
        return _queue_singleton

    from app.core.redis import get_redis

    last_exc: Exception | None = None
    for attempt in range(max(retries, 1)):
        try:
            client = get_redis()
            await client.ping()
            _queue_singleton = RedisStreamQueue(client)
            logger.info("run queue: Redis Streams transport (stream=%s)", settings.RUN_QUEUE_STREAM)
            return _queue_singleton
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < max(retries, 1):
                await asyncio.sleep(retry_delay)
    raise RunQueueUnavailable(
        f"durable run queue requires Redis but it is unreachable: {last_exc}"
    ) from last_exc


def set_run_queue(queue: RunQueue | None) -> None:
    """Inject a queue (testing / forced override). Pass ``None`` to reset."""
    global _queue_singleton
    _queue_singleton = queue
