"""The durable ingestion queue: leases, attempts, backoff, takeover.

Everything here is about what happens when the process that accepted an upload
stops being available. The old shape of that story was: a process-local
background task dies with the process, the document stays ``parsing`` forever,
and a time-based sweeper re-dispatched it — including while a live worker was
still indexing it, and without bound for a file that never parses.

The queue replaces "guess from the clock" with state on the row: who owns it and
until when (:func:`claim` / :func:`renew`), how many tries it has left
(``ingest_attempts``), and when the next one may start
(``ingest_next_retry_at``). Each of those is asserted below against a real
SQLite database, because the whole mechanism is conditional UPDATEs whose
correctness is exactly "two racers, one winner".
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import Document, KnowledgeBase
from app.services import document_service
from app.services.ingestion_queue import (
    IngestionOutcome,
    IngestionWorker,
    backoff_seconds,
    claim,
    enqueue,
    finish,
    renew,
)
from tests.conftest import TestSessionLocal

SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _now() -> datetime:
    """A naive-UTC instant, comparable with what either engine hands back.

    SQLite's DATETIME has no tzinfo (it stores and returns the UTC wall time),
    Postgres returns an aware value for ``TIMESTAMPTZ`` — so normalize on read
    instead of writing the same assertion twice.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def _as_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


def _settings(**over) -> SimpleNamespace:
    """A settings stand-in: the queue reads five knobs and nothing else."""
    base = {
        "INGEST_LEASE_TTL_SECONDS": 300,
        "INGEST_LEASE_RENEW_SECONDS": 60,
        "INGEST_MAX_ATTEMPTS": 3,
        "INGEST_BACKOFF_BASE_SECONDS": 30,
        "INGEST_POLL_INTERVAL_SECONDS": 0.05,
    }
    base.update(over)
    return SimpleNamespace(**base)


async def _doc(
    db,
    *,
    status: str = "pending",
    name: str = "doc",
    created_at: datetime | None = None,
    **columns,
) -> Document:
    kb = KnowledgeBase(user_id=SEEDED_USER, name=f"{name}-kb-{uuid.uuid4().hex[:6]}")
    db.add(kb)
    await db.flush()
    doc = Document(
        knowledge_base_id=kb.id,
        filename=f"{name}.txt",
        file_path=f"/tmp/{name}-{uuid.uuid4().hex[:6]}.txt",
        file_type=".txt",
        status=status,
        **columns,
    )
    if created_at is not None:
        doc.created_at = created_at
    db.add(doc)
    await db.commit()
    return doc


async def _row(db, document_id) -> Document | None:
    """Re-read the row through the same session.

    ``TestSessionLocal`` is ``expire_on_commit=False`` and every session here
    shares one SQLite connection, so ``db.get`` can hand back a cached object; a
    SELECT always re-reads what the conditional UPDATE just changed.

    ``populate_existing`` is not redundant with that: the queue's bulk UPDATEs
    synchronize the identity map, so a row this session has already loaded keeps
    its *previous* attributes unless the read forces them, and a plain SELECT
    asserts the cache rather than the database.
    """
    return (
        await db.execute(
            select(Document)
            .where(Document.id == document_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


@pytest.fixture(autouse=True)
async def _park_foreign_documents(db_session):
    """Put every document this file did not create out of the queue's reach.

    ``claim`` is global by design, and the session database is shared across
    test files, so a leftover ``pending`` row from another file would otherwise
    be claimed — and re-stated — by these tests. Parking (a far-future backoff)
    is not deleting: the other rows are still there for whoever owns them.
    """
    from sqlalchemy import update

    await db_session.execute(
        update(Document)
        .where(Document.status.in_(("pending", "parsing", "chunking", "embedding")))
        .values(ingest_next_retry_at=datetime.now(UTC) + timedelta(days=3650))
    )
    await db_session.commit()
    yield


# --------------------------------------------------------------------------- #
# Pure policy
# --------------------------------------------------------------------------- #
def test_backoff_grows_and_is_capped():
    assert backoff_seconds(1, 30) == 30
    assert backoff_seconds(2, 30) == 60
    assert backoff_seconds(3, 30) == 120
    assert backoff_seconds(30, 30) == 1800  # ceiling: a fix must not need hours


async def test_claim_is_oldest_first(db_session):
    """Fairness matters when a sweep is requeuing a backlog: a young document
    must not index ahead of one that has been waiting since before the restart."""
    old = await _doc(db_session, name="old", created_at=_now() - timedelta(days=2))
    new = await _doc(db_session, name="new")
    assert await claim(db_session, worker_id="w1", settings=_settings()) == old.id
    assert await claim(db_session, worker_id="w1", settings=_settings()) == new.id


# --------------------------------------------------------------------------- #
# claim: who may start, and what starting costs
# --------------------------------------------------------------------------- #
async def test_claim_sets_lease_owner_and_counts_the_attempt(db_session):
    doc = await _doc(db_session)
    claimed = await claim(db_session, worker_id="w1", settings=_settings())
    assert claimed == doc.id
    row = await _row(db_session, doc.id)
    assert row.status == "parsing"
    assert row.ingest_claimed_by == "w1"
    assert row.ingest_attempts == 1
    assert _as_naive(row.ingest_lease_expires_at) > _now() - timedelta(minutes=4)


async def test_terminal_statuses_are_not_claimable(db_session):
    await _doc(db_session, name="ok", status="indexed")
    await _doc(db_session, name="bad", status="failed")
    assert await claim(db_session, worker_id="w1", settings=_settings()) is None


async def test_a_live_lease_blocks_every_other_worker(db_session):
    """This is the guard the clock-based sweeper could not offer: two processes
    indexing one document double-write its chunks and fight over its status."""
    doc = await _doc(db_session)
    assert await claim(db_session, worker_id="w1", settings=_settings()) == doc.id
    assert await claim(db_session, worker_id="w2", settings=_settings()) is None


async def test_an_expired_lease_is_taken_over(db_session):
    """Crash recovery: the owner stopped renewing, so the job is free again."""
    doc = await _doc(db_session)
    await claim(db_session, worker_id="dead-worker", settings=_settings())
    await db_session.execute(
        Document.__table__.update()
        .where(Document.id == doc.id)
        .values(ingest_lease_expires_at=_now() - timedelta(seconds=1))
    )
    await db_session.commit()
    assert await claim(db_session, worker_id="w2", settings=_settings()) == doc.id
    row = await _row(db_session, doc.id)
    assert row.ingest_claimed_by == "w2"
    # The dead attempt still counts, or a crash loop would never run out.
    assert row.ingest_attempts == 2


async def test_a_document_before_its_backoff_window_is_skipped(db_session):
    doc = await _doc(db_session)
    await db_session.execute(
        Document.__table__.update()
        .where(Document.id == doc.id)
        .values(ingest_next_retry_at=_now() + timedelta(minutes=5))
    )
    await db_session.commit()
    assert await claim(db_session, worker_id="w1", settings=_settings()) is None


async def test_a_stranded_row_without_a_lease_is_claimable(db_session):
    """Rows created before the queue existed (or by a crash between the status
    write and anything else) need no backfill: a NULL lease means nobody owns
    it, which is precisely the state a takeover wants."""
    doc = await _doc(db_session, status="parsing")
    assert await claim(db_session, worker_id="w1", settings=_settings()) == doc.id


async def test_a_second_session_cannot_double_claim(db_session):
    """Two workers, one document, exactly one claim.

    The decision is the conditional UPDATE's rowcount, so it does not depend on
    who read the row first. (A literal ``asyncio.gather`` race cannot be
    expressed here: the test engine is one shared SQLite connection, so the two
    sessions are serialised by the pool and would prove nothing.)
    """
    doc = await _doc(db_session)
    async with TestSessionLocal() as first:
        assert await claim(first, worker_id="w1", settings=_settings()) == doc.id
    async with TestSessionLocal() as second:
        assert await claim(second, worker_id="w2", settings=_settings()) is None
    row = await _row(db_session, doc.id)
    assert row.ingest_attempts == 1  # not 2
    assert row.ingest_claimed_by == "w1"


# --------------------------------------------------------------------------- #
# renew / finish
# --------------------------------------------------------------------------- #
async def test_renew_extends_only_its_own_lease(db_session):
    doc = await _doc(db_session)
    await claim(db_session, worker_id="w1", settings=_settings())
    assert await renew(db_session, doc.id, worker_id="w1", settings=_settings()) is True
    assert await renew(db_session, doc.id, worker_id="w2", settings=_settings()) is False
    # An expired lease cannot be resurrected by the worker that lost it.
    await db_session.execute(
        Document.__table__.update()
        .where(Document.id == doc.id)
        .values(ingest_lease_expires_at=_now() - timedelta(seconds=1))
    )
    await db_session.commit()
    assert await renew(db_session, doc.id, worker_id="w1", settings=_settings()) is False


async def test_finish_done_clears_the_lease_and_keeps_history(db_session):
    doc = await _doc(db_session)
    await claim(db_session, worker_id="w1", settings=_settings())
    await db_session.execute(
        Document.__table__.update().where(Document.id == doc.id).values(status="indexed")
    )
    await db_session.commit()
    verdict = await finish(
        db_session, doc.id, worker_id="w1", outcome=IngestionOutcome(ok=True), settings=_settings()
    )
    assert verdict == "done"
    row = await _row(db_session, doc.id)
    assert (row.status, row.ingest_claimed_by, row.ingest_lease_expires_at) == (
        "indexed",
        None,
        None,
    )
    assert row.ingest_attempts == 1  # kept: "how many tries did this take" is data


async def test_finish_failure_requeues_with_backoff(db_session):
    doc = await _doc(db_session)
    await claim(db_session, worker_id="w1", settings=_settings())
    verdict = await finish(
        db_session,
        doc.id,
        worker_id="w1",
        outcome=IngestionOutcome(ok=False, error="embedding 上游 503"),
        settings=_settings(),
    )
    assert verdict == "retry"
    row = await _row(db_session, doc.id)
    assert row.status == "pending"
    assert row.ingest_claimed_by is None and row.ingest_lease_expires_at is None
    # The error must survive: a document that fails silently is undiagnosable.
    assert "503" in row.error_message
    wait = _as_naive(row.ingest_next_retry_at) - _now()
    assert timedelta(seconds=20) < wait <= timedelta(seconds=31)


async def test_attempts_run_out_and_the_document_fails_for_good(db_session):
    settings = _settings()
    doc = await _doc(db_session)
    verdicts = []
    for attempt in range(3):
        assert await claim(db_session, worker_id="w1", settings=settings) == doc.id
        verdicts.append(
            await finish(
                db_session,
                doc.id,
                worker_id="w1",
                outcome=IngestionOutcome(ok=False, error=f"boom {attempt}"),
                settings=settings,
            )
        )
        # The retry window is real (asserted above); erase it to keep looping.
        await db_session.execute(
            Document.__table__.update()
            .where(Document.id == doc.id)
            .values(ingest_next_retry_at=None)
        )
        await db_session.commit()
    assert verdicts == ["retry", "retry", "failed"]
    row = await _row(db_session, doc.id)
    assert row.status == "failed"
    assert row.ingest_attempts == 3
    # No further retry is scheduled: the sweeper must not resurrect it either.
    assert row.ingest_next_retry_at is None
    assert await claim(db_session, worker_id="w1", settings=settings) is None


async def test_a_non_retryable_failure_does_not_burn_the_attempts(db_session):
    """An empty file cannot be fixed by trying again; the attempts are for
    infrastructure that is briefly down."""
    doc = await _doc(db_session)
    await claim(db_session, worker_id="w1", settings=_settings())
    verdict = await finish(
        db_session,
        doc.id,
        worker_id="w1",
        outcome=IngestionOutcome(ok=False, error="文档内容为空或无法解析", retryable=False),
        settings=_settings(),
    )
    assert verdict == "failed"
    assert (await _row(db_session, doc.id)).status == "failed"


async def test_a_lost_claim_cannot_write_its_verdict(db_session):
    """Split-brain guard: worker 1 came back late after worker 2 took over.

    Without this, the late worker's success/failure would overwrite the state of
    a document it no longer owns — including a ``failed`` that hides a healthy
    index worker 2 just completed.
    """
    doc = await _doc(db_session)
    await claim(db_session, worker_id="w1", settings=_settings())
    await db_session.execute(
        Document.__table__.update()
        .where(Document.id == doc.id)
        .values(ingest_lease_expires_at=_now() - timedelta(seconds=1))
    )
    await db_session.commit()
    assert await claim(db_session, worker_id="w2", settings=_settings()) == doc.id

    verdict = await finish(
        db_session,
        doc.id,
        worker_id="w1",
        outcome=IngestionOutcome(ok=False, error="late failure"),
        settings=_settings(),
    )
    assert verdict == "lost"
    row = await _row(db_session, doc.id)
    assert row.ingest_claimed_by == "w2"
    assert row.status == "parsing"
    assert row.error_message is None


async def test_finish_of_a_deleted_document_is_harmless(db_session):
    doc = await _doc(db_session)
    await claim(db_session, worker_id="w1", settings=_settings())
    await db_session.execute(Document.__table__.delete().where(Document.id == doc.id))
    await db_session.commit()
    verdict = await finish(
        db_session, doc.id, worker_id="w1", outcome=IngestionOutcome(ok=True), settings=_settings()
    )
    assert verdict == "gone"


# --------------------------------------------------------------------------- #
# enqueue (the API's whole side of the contract)
# --------------------------------------------------------------------------- #
async def test_enqueue_requeues_a_failed_document_and_resets_attempts(db_session):
    doc = await _doc(db_session, status="failed", ingest_attempts=7, error_message="gone")
    await db_session.execute(
        Document.__table__.update()
        .where(Document.id == doc.id)
        .values(
            # The backoff window a manual reindex has to override, plus the
            # owner/lease a dead worker left behind (an *expired* one — a live
            # lease means someone is indexing right now, see the test below).
            ingest_next_retry_at=_now() + timedelta(hours=1),
            ingest_lease_expires_at=_now() - timedelta(hours=1),
            ingest_claimed_by="someone",
        )
    )
    await db_session.commit()
    assert await enqueue(db_session, doc.id) is True
    row = await _row(db_session, doc.id)
    assert (row.status, row.ingest_attempts, row.ingest_claimed_by) == ("pending", 0, None)
    assert row.ingest_next_retry_at is None and row.error_message is None
    assert await claim(db_session, worker_id="w1", settings=_settings()) == doc.id


async def test_enqueue_refuses_a_document_another_worker_holds(db_session):
    """Reindex during a running ingestion must not hand the file to a second worker.

    Clearing the lease is exactly the double-index the lease exists to prevent, so
    the enqueue is a conditional UPDATE on it and the caller gets a refusal (the
    route turns that into a 409).
    """
    doc = await _doc(db_session)
    await claim(db_session, worker_id="w1", settings=_settings())
    assert await enqueue(db_session, doc.id) is False
    row = await _row(db_session, doc.id)
    assert (row.status, row.ingest_claimed_by) == ("parsing", "w1")

    # Once the lease expires the same call succeeds: takeover is the point.
    await db_session.execute(
        Document.__table__.update()
        .where(Document.id == doc.id)
        .values(ingest_lease_expires_at=_now() - timedelta(seconds=1))
    )
    await db_session.commit()
    assert await enqueue(db_session, doc.id) is True
    assert await claim(db_session, worker_id="w2", settings=_settings()) == doc.id


async def test_enqueue_without_reset_keeps_the_attempt_count(db_session):
    """Manual re-requeue must not hand a poison file an unbounded second life."""
    doc = await _doc(db_session, status="failed", ingest_attempts=3)
    assert await enqueue(db_session, doc.id, reset_attempts=False) is True
    assert (await _row(db_session, doc.id)).ingest_attempts == 3


# --------------------------------------------------------------------------- #
# The worker loop
# --------------------------------------------------------------------------- #
async def test_worker_processes_one_document_end_to_end(db_session):
    doc = await _doc(db_session)
    seen: list[uuid.UUID] = []

    async def _index(db, document_id):
        """Behave like the real pipeline: it owns the terminal status."""
        seen.append(document_id)
        await db.execute(
            Document.__table__.update()
            .where(Document.id == document_id)
            .values(status="indexed")
        )
        await db.commit()
        return IngestionOutcome(ok=True)

    worker = IngestionWorker(
        _index, session_factory=TestSessionLocal, settings=_settings(), worker_id="w1"
    )
    assert await worker.run_once() == (doc.id, "done")
    assert seen == [doc.id]
    assert await worker.run_once() is None  # queue is empty
    row = await _row(db_session, doc.id)
    assert row.status == "indexed"
    assert row.ingest_claimed_by is None and row.ingest_lease_expires_at is None


async def test_worker_turns_a_crashing_pipeline_into_bounded_retries(db_session):
    """An ``index_document`` that raises (integration bug, OOM-killed mid-call)
    must not strand the row in ``parsing`` forever, and must not loop either."""
    doc = await _doc(db_session)
    calls = {"n": 0}

    async def _explode(_db, document_id):
        calls["n"] += 1
        # Leave the row mid-flight, exactly like a process that died here.
        raise RuntimeError("pipeline exploded")

    worker = IngestionWorker(
        _explode, session_factory=TestSessionLocal, settings=_settings(), worker_id="w1"
    )
    verdicts = []
    for _ in range(4):
        result = await worker.run_once()
        if result is None:
            break  # backoff window: nothing claimable yet
        verdicts.append(result[1])
        await db_session.execute(
            Document.__table__.update()
            .where(Document.id == doc.id)
            .values(ingest_next_retry_at=None)
        )
        await db_session.commit()
    assert verdicts == ["retry", "retry", "failed"]
    assert calls["n"] == 3
    row = await _row(db_session, doc.id)
    assert row.status == "failed" and row.ingest_attempts == 3
    assert "exploded" in row.error_message


async def test_worker_drains_the_queue_and_stops(db_session):
    """``start()`` polls until it is asked to stop, then leaves no task behind.

    The wait watches the stub's call log in memory rather than reading the
    database: the test engine is a single shared connection, so a read from the
    test would interleave with the worker's own queries — that is a limit of the
    harness, not of the queue (Postgres gives every worker its own connection).
    Checking the rows only after ``stop()`` keeps the assertion honest.
    """
    first = await _doc(db_session, name="a")
    second = await _doc(db_session, name="b")
    seen: list[uuid.UUID] = []

    async def _index(db, document_id):
        seen.append(document_id)
        await db.execute(
            Document.__table__.update()
            .where(Document.id == document_id)
            .values(status="indexed")
        )
        await db.commit()
        return IngestionOutcome(ok=True)

    worker = IngestionWorker(
        _index, session_factory=TestSessionLocal, settings=_settings(), worker_id="w1"
    )
    worker.start()
    try:
        for _ in range(100):
            if len(seen) >= 2:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail(f"queue did not drain: {seen}")
    finally:
        await worker.stop()
    assert worker._task is None  # the loop really exited, no task left behind
    assert {d.id for d in (first, second)} == set(seen)
    statuses = list(
        (
            await db_session.execute(
                select(Document.status).where(Document.id.in_([first.id, second.id]))
            )
        )
        .scalars()
        .all()
    )
    assert statuses == ["indexed", "indexed"]


# --------------------------------------------------------------------------- #
# The pipeline's own retryable/not classification
# --------------------------------------------------------------------------- #
async def test_index_document_reports_an_unparseable_file_as_terminal(db_session, tmp_path):
    """A file with no text is a property of the row, not of the infrastructure."""
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    doc = await _doc(db_session, name="empty")
    doc.file_path = str(empty)
    await db_session.commit()

    outcome = await document_service.index_document(db_session, doc.id)
    assert outcome.ok is False
    assert outcome.retryable is False
    assert (await _row(db_session, doc.id)).status == "failed"


async def test_index_document_reports_a_missing_embedding_model_as_retryable(
    db_session, monkeypatch, tmp_path
):
    """An unconfigured/暂时挂掉的 embedding 模型正是「重试」存在的理由：运维几秒后
    补上，文档就该自己索引完，而不是变成一条需要人工重建的 failed。"""
    from app.services import document_service as ds

    body = tmp_path / "body.txt"
    body.write_text("可用的中文正文，长度足够切成块用于测试。" * 20, encoding="utf-8")
    doc = await _doc(db_session, name="missing-model")
    doc.file_path = str(body)
    await db_session.commit()

    async def _no_model(_db, _kb):
        raise RuntimeError("No embedding model is configured")

    monkeypatch.setattr(ds, "_resolve_embedding_config", _no_model)
    outcome = await document_service.index_document(db_session, doc.id)
    assert outcome.ok is False
    assert outcome.retryable is True
    # The row says failed either way: only the queue knows whether to retry.
    assert (await _row(db_session, doc.id)).status == "failed"
