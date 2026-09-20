"""Durable ingestion queue for knowledge-base documents.

Before this module, ingestion ran as a process-local background task in
whichever process happened to accept the upload. Two consequences, both real:

* a deploy or crash destroyed the task and the row stayed ``pending``/``parsing``
  forever (the time-based ``StaleJobSweeper`` could re-dispatch it, but with no
  notion of who was running it — two processes could index the same document);
* a file that keeps failing was re-dispatched on every sweep, forever.

So the queue has leases (with an owner, so a taken-over job cannot be written
back by the process that lost it), a bounded attempt count, and exponential
backoff between attempts.

The ``Document`` row *is* the job: ``status`` is the state machine and the four
``ingest_*`` columns are the scheduling metadata (see app/models/document.py).
A separate table would add a second source of truth that can drift from the
document it describes, and every enqueue would have to keep both in step. It
also means a row created by older code is already in the queue — ``claim``
accepts ``ingest_lease_expires_at IS NULL``.

Every step is a short conditional UPDATE rather than a read-then-write, so two
workers racing for one document is decided by the database (``rowcount``), not
by whoever checked first.

Those UPDATEs all carry ``synchronize_session=False``. They are the queue's own
state transitions, and letting the ORM re-evaluate the lease predicates against
whatever rows the calling session happens to have loaded is wasted work at best:
Python cannot compare a loaded ``None`` lease with a timestamp, so the default
``'auto'`` strategy raises ``TypeError`` in an API session that just read the
document it is queueing. Callers that need the new state re-read the row.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models import Document

logger = logging.getLogger(__name__)

# Non-terminal statuses: a row in one of these is a job that exists.
QUEUED_STATUS = "pending"
_IN_PROGRESS_STATUSES = ("parsing", "chunking", "embedding")
CLAIMABLE_STATUSES = (QUEUED_STATUS, *_IN_PROGRESS_STATUSES)

# Candidate ids scanned per claim. Whichever UPDATE claims first wins; the rest
# are simply still owned by someone else.
_CLAIM_SCAN = 10

# Backoff ceiling: 2**n grows without bound, and an operator fixing an outage
# should not have to wait hours before the next attempt.
_BACKOFF_CAP_SECONDS = 1800


@dataclass(frozen=True)
class IngestionOutcome:
    """What one ingestion run decided. ``retryable`` is the pipeline's call.

    A corrupt/empty file is terminal the first time (retrying cannot change its
    bytes); an embedding-provider outage is transient. Without this split, a
    permanently bad file burns every attempt and a transient one may be given up
    on too early.
    """

    ok: bool
    error: str | None = None
    retryable: bool = True


IndexFn = Callable[[AsyncSession, uuid.UUID], Awaitable[IngestionOutcome]]


def _now() -> datetime:
    return datetime.now(UTC)


def backoff_seconds(attempts: int, base_seconds: int) -> int:
    """Wait before attempt ``attempts + 1``: ``base * 2**(attempts - 1)``, capped.

    ``attempts`` is the count already spent (1 for the first try), so the first
    retry waits ``base`` and the growth starts immediately.
    """
    if attempts < 1:
        return int(base_seconds)
    return min(int(base_seconds) * (2 ** (attempts - 1)), _BACKOFF_CAP_SECONDS)


def _knobs(settings: Settings | None) -> dict[str, Any]:
    s = settings or get_settings()
    return {
        "ttl": max(1, int(getattr(s, "INGEST_LEASE_TTL_SECONDS", 300))),
        "renew": max(1, int(getattr(s, "INGEST_LEASE_RENEW_SECONDS", 60))),
        "max_attempts": max(1, int(getattr(s, "INGEST_MAX_ATTEMPTS", 4))),
        "backoff": max(1, int(getattr(s, "INGEST_BACKOFF_BASE_SECONDS", 30))),
        "poll": max(0.05, float(getattr(s, "INGEST_POLL_INTERVAL_SECONDS", 3.0))),
    }


def _claimable_predicate(now: datetime) -> tuple:
    """The WHERE both the candidate SELECT and the claim UPDATE must share.

    Re-checking it in the UPDATE is what makes the claim atomic: the row can
    have been taken over between the SELECT and the UPDATE.
    """
    free_lease = or_(
        Document.ingest_lease_expires_at.is_(None),
        Document.ingest_lease_expires_at <= now,
    )
    due = or_(
        Document.ingest_next_retry_at.is_(None),
        Document.ingest_next_retry_at <= now,
    )
    return (Document.status.in_(CLAIMABLE_STATUSES), free_lease, due)


async def enqueue(
    db: AsyncSession,
    document_id: uuid.UUID,
    *,
    reset_attempts: bool = True,
) -> bool:
    """(Re)queue a document: the only thing an upload/reindex has to do.

    ``reset_attempts=False`` keeps the attempt count, so a manual re-requeue of
    a document that already failed ``INGEST_MAX_ATTEMPTS`` times does not buy
    the poison file an unbounded second life.

    Returns ``False`` — and changes nothing — when a worker currently holds a
    live lease. Clearing that lease would hand the same document to a second
    worker while the first is still embedding it, which is the double-index (and
    duplicated-vector) bug the lease exists to prevent, so a reindex pressed
    during a running ingestion is refused rather than queued. The row is
    likewise absent if the document was deleted under us.
    """
    values: dict[str, Any] = {
        "status": QUEUED_STATUS,
        "error_message": None,
        "ingest_next_retry_at": None,
        "ingest_lease_expires_at": None,
        "ingest_claimed_by": None,
    }
    if reset_attempts:
        values["ingest_attempts"] = 0
    result = await db.execute(
        update(Document)
        .where(
            Document.id == document_id,
            or_(
                Document.ingest_lease_expires_at.is_(None),
                Document.ingest_lease_expires_at <= _now(),
            ),
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if result.rowcount != 1:
        logger.info(
            "document %s is being ingested by someone else (or is gone); not re-queued",
            document_id,
        )
        return False
    return True


async def claim(
    db: AsyncSession,
    *,
    worker_id: str,
    settings: Settings | None = None,
) -> uuid.UUID | None:
    """Take ownership of one queued document, or return None if nothing is free.

    Also counts the attempt, so a worker that claims and then dies cannot be
    picked up forever without progress.
    """
    knobs = _knobs(settings)
    now = _now()
    predicate = _claimable_predicate(now)
    candidate_ids = list(
        (
            await db.execute(
                select(Document.id)
                .where(*predicate)
                .order_by(Document.created_at.asc())
                .limit(_CLAIM_SCAN)
            )
        )
        .scalars()
        .all()
    )
    expires_at = now + timedelta(seconds=knobs["ttl"])
    claimed: uuid.UUID | None = None
    for document_id in candidate_ids:
        result = await db.execute(
            update(Document)
            .where(Document.id == document_id, *_claimable_predicate(_now()))
            .values(
                status="parsing",
                ingest_lease_expires_at=expires_at,
                ingest_claimed_by=worker_id,
                ingest_attempts=Document.ingest_attempts + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            claimed = document_id
            break
    await db.commit()
    return claimed


async def renew(
    db: AsyncSession,
    document_id: uuid.UUID,
    *,
    worker_id: str,
    settings: Settings | None = None,
) -> bool:
    """Extend our own unexpired lease. ``False`` means someone else owns it.

    Renewal is what lets the lease be short: crash detection is fast without a
    slow file being stolen mid-run.
    """
    knobs = _knobs(settings)
    now = _now()
    result = await db.execute(
        update(Document)
        .where(
            Document.id == document_id,
            Document.ingest_claimed_by == worker_id,
            Document.ingest_lease_expires_at > now,
        )
        .values(ingest_lease_expires_at=now + timedelta(seconds=knobs["ttl"]))
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount == 1


async def finish(
    db: AsyncSession,
    document_id: uuid.UUID,
    *,
    worker_id: str,
    outcome: IngestionOutcome,
    settings: Settings | None = None,
) -> str:
    """Close out a claim: ``done`` | ``retry`` | ``failed`` | ``lost`` | ``gone``.

    ``lost`` is the split-brain guard: if another process took the document over
    (our lease expired and was claimed), writing our verdict would clobber
    its state, so we leave the row to it.
    """
    knobs = _knobs(settings)
    # Read the columns, not the ORM entity: a session that just claimed this row
    # can still hold a cached copy of it, and ``claimed_by`` is exactly the field
    # whose staleness would decide the wrong verdict here.
    state = (
        await db.execute(
            select(Document.ingest_attempts, Document.ingest_claimed_by).where(
                Document.id == document_id
            )
        )
    ).first()
    if state is None:
        # Deleted while indexing; the chunk/vector cleanup already ran.
        return "gone"
    attempts, owner = state
    if owner != worker_id:
        logger.warning(
            "ingestion of document %s was taken over by %s; %s is dropping its result",
            document_id,
            owner,
            worker_id,
        )
        return "lost"

    values: dict[str, Any] = {
        "ingest_lease_expires_at": None,
        "ingest_claimed_by": None,
    }
    if outcome.ok:
        values["ingest_next_retry_at"] = None
    elif outcome.retryable and attempts < knobs["max_attempts"]:
        values["status"] = QUEUED_STATUS
        values["error_message"] = (outcome.error or "摄取失败")[:500]
        values["ingest_next_retry_at"] = _now() + timedelta(
            seconds=backoff_seconds(attempts, knobs["backoff"])
        )
    else:
        values["status"] = "failed"
        values["error_message"] = (outcome.error or "摄取失败")[:500]
        values["ingest_next_retry_at"] = None

    # The owner guard is repeated in the WHERE: if the lease was taken over
    # between the read and here, this matches nothing and their state stands.
    result = await db.execute(
        update(Document)
        .where(Document.id == document_id, Document.ingest_claimed_by == worker_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if result.rowcount != 1:
        return "lost"

    if outcome.ok:
        return "done"
    if values["status"] == QUEUED_STATUS:
        logger.warning(
            "document %s attempt %d/%d failed (%s); retrying at %s",
            document_id,
            attempts,
            knobs["max_attempts"],
            (outcome.error or "")[:200],
            values["ingest_next_retry_at"],
        )
        return "retry"
    logger.error(
        "document %s failed permanently after %d attempt(s): %s",
        document_id,
        attempts,
        (outcome.error or "")[:200],
    )
    return "failed"


class IngestionWorker:
    """claim -> execute (lease renewed) -> finish, one document at a time."""

    def __init__(
        self,
        index_fn: IndexFn,
        *,
        session_factory: Callable[[], Any],
        worker_id: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._index_fn = index_fn
        self._session_factory = session_factory
        self.worker_id = worker_id or f"ingest-{uuid.uuid4().hex[:8]}"
        self._settings = settings
        self._knobs = _knobs(settings)
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def notify(self) -> None:
        """Wake the poll loop so an upload does not wait out the interval."""
        self._wake.set()

    async def run_once(self) -> tuple[uuid.UUID, str] | None:
        """Process at most one document. Returns ``(document_id, verdict)``."""
        async with self._session_factory() as db:
            document_id = await claim(db, worker_id=self.worker_id, settings=self._settings)
        if document_id is None:
            return None

        heartbeat = asyncio.create_task(self._heartbeat(document_id))
        try:
            outcome = await self._execute(document_id)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass

        async with self._session_factory() as db:
            verdict = await finish(
                db,
                document_id,
                worker_id=self.worker_id,
                outcome=outcome,
                settings=self._settings,
            )
        return document_id, verdict

    async def _execute(self, document_id: uuid.UUID) -> IngestionOutcome:
        """Run the pipeline, turning any raise into a retryable outcome.

        A lease lost mid-run cannot be un-started (the pipeline writes the row
        as it goes); what the lease buys is that *nobody else* is waiting an
        unbounded time for it, and :func:`finish` makes sure the loser does not
        then overwrite the new owner's verdict.
        """
        try:
            async with self._session_factory() as db:
                return await self._index_fn(db, document_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the pipeline may raise on an integration bug
            logger.exception("ingestion worker %s crashed on %s", self.worker_id, document_id)
            return IngestionOutcome(ok=False, error=str(exc), retryable=True)

    async def _heartbeat(self, document_id: uuid.UUID) -> None:
        """Renew the lease while the pipeline runs.

        Losing it is logged, not interrupted: :func:`finish` is what refuses to
        write a verdict onto a row another worker now owns.
        """
        interval = min(self._knobs["renew"], max(1, self._knobs["ttl"] // 3))
        while True:
            await asyncio.sleep(interval)
            try:
                async with self._session_factory() as db:
                    held = await renew(
                        db, document_id, worker_id=self.worker_id, settings=self._settings
                    )
            except Exception:  # a blip must not look like a lost lease
                logger.exception("ingestion lease renewal failed for %s", document_id)
                continue
            if not held:
                logger.warning("lost the lease on document %s", document_id)
                return

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        """Poll loop until stopped. Idle waits are cut short by :meth:`notify`."""
        stop = stop_event or self._stop
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:  # pragma: no cover - the loop must outlive one bad job
                logger.exception("ingestion worker loop iteration failed")
            if self._wake.is_set():
                # An upload landed while we were working: keep draining, no sleep.
                self._wake.clear()
                continue
            try:
                await asyncio.wait_for(
                    _either(stop, self._wake), timeout=self._knobs["poll"]
                )
            except TimeoutError:
                pass
            self._wake.clear()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run_forever())

    async def stop(self, *, timeout: float = 10.0) -> None:
        """Ask the loop to finish its current checkpoint and wait for it.

        Graceful first on purpose: cancelling mid-``run_once`` throws away a
        document that is halfway through indexing. The queue survives that (the
        lease expires and another worker takes over), but the work is gone, and a
        shutdown that always cancels turns every deploy into a re-index of
        whatever was in flight. The cancel is only the fallback for a wedged loop.
        """
        self._stop.set()
        self._wake.set()  # cut short an idle wait instead of sleeping out the poll
        if self._task is not None:
            try:
                # On timeout this cancels the task, which the loop turns into an
                # exit; the row it was holding is then healed by the lease.
                await asyncio.wait_for(self._task, timeout=timeout)
            except (TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):  # pragma: no cover
                    pass
            self._task = None


async def _either(*events: asyncio.Event) -> None:
    """Wait for the first of several events (no single helper exists for that)."""
    tasks = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()


# --------------------------------------------------------------------------- #
# The in-process worker the API wakes when it accepts an upload.
#
# The queue itself is durable, so this is a latency optimisation, not the
# execution guarantee: a deployment may run the loop in the worker process
# instead, and an API process that never starts one still enqueues correctly.
# --------------------------------------------------------------------------- #
_active_worker: IngestionWorker | None = None


def set_active_worker(worker: IngestionWorker | None) -> None:
    global _active_worker
    _active_worker = worker


def get_active_worker() -> IngestionWorker | None:
    return _active_worker


def notify_ingestion_worker() -> bool:
    """Hand a freshly enqueued document to the local loop. False if none runs."""
    worker = _active_worker
    if worker is None:
        return False
    worker.notify()
    return True


async def build_worker() -> IngestionWorker:
    """The production worker: real session factory + real ingestion pipeline."""
    from app.db import AsyncSessionLocal
    from app.services import document_service

    return IngestionWorker(
        document_service.index_document,
        session_factory=AsyncSessionLocal,
    )
