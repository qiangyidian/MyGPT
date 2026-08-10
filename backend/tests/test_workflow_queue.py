"""Durable run queue: InMemoryQueue transport (Task 5).

Tests the queue contract that both transports must satisfy:
  * enqueue is idempotent (same run_id twice -> one pending entry)
  * pending_ids reflects un-claimed entries in FIFO order
  * dequeue claims the next entry for an owner
  * ack removes the claimed entry
  * a run already in-flight is not re-enqueued
"""
from __future__ import annotations

import uuid

import pytest

from app.agents.events import EventStore
from app.agents.schemas import AgentEvent
from app.agents.workflow.queue import InMemoryQueue, get_run_queue
from app.agents.workflow.repository import LeaseStore
from app.agents.workflow.worker import RunWorker
from app.models import AgentRun, Conversation, Message

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _make_run(db_session, *, status: str = "running") -> AgentRun:
    conv = Conversation(user_id=_SEEDED_USER, title="queue-test")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id,
        message_id=msg.id,
        user_id=_SEEDED_USER,
        runtime="native",
        flow_name="test",
        status=status,
    )
    db_session.add(run)
    await db_session.flush()
    return run


# --------------------------------------------------------------------------- #
# Idempotent enqueue
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_enqueue_same_run_is_idempotent():
    queue = InMemoryQueue()
    run_id = uuid.uuid4()
    await queue.enqueue(run_id)
    await queue.enqueue(run_id)
    assert await queue.pending_ids() == [run_id]


@pytest.mark.asyncio
async def test_enqueue_different_runs_kept_in_order():
    queue = InMemoryQueue()
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await queue.enqueue(a)
    await queue.enqueue(b)
    await queue.enqueue(c)
    assert await queue.pending_ids() == [a, b, c]


@pytest.mark.asyncio
async def test_enqueue_str_run_id_normalised():
    queue = InMemoryQueue()
    run_id = uuid.uuid4()
    await queue.enqueue(str(run_id))
    await queue.enqueue(run_id)  # same id as UUID
    assert await queue.pending_ids() == [run_id]


# --------------------------------------------------------------------------- #
# dequeue / claim
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_dequeue_returns_oldest_pending():
    queue = InMemoryQueue()
    a, b = uuid.uuid4(), uuid.uuid4()
    await queue.enqueue(a)
    await queue.enqueue(b)
    claimed = await queue.dequeue(owner="worker-1")
    assert claimed == a


@pytest.mark.asyncio
async def test_dequeue_empty_returns_none():
    queue = InMemoryQueue()
    assert await queue.dequeue(owner="worker-1") is None


@pytest.mark.asyncio
async def test_dequeue_removes_from_pending():
    queue = InMemoryQueue()
    run_id = uuid.uuid4()
    await queue.enqueue(run_id)
    await queue.dequeue(owner="worker-1")
    assert await queue.pending_ids() == []


# --------------------------------------------------------------------------- #
# ack
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ack_removes_in_flight():
    queue = InMemoryQueue()
    run_id = uuid.uuid4()
    await queue.enqueue(run_id)
    await queue.dequeue(owner="worker-1")
    await queue.ack(run_id, owner="worker-1")
    # After ack the run is gone: re-enqueue is allowed and appears in pending.
    await queue.enqueue(run_id)
    assert await queue.pending_ids() == [run_id]


@pytest.mark.asyncio
async def test_enqueue_blocked_while_in_flight():
    queue = InMemoryQueue()
    run_id = uuid.uuid4()
    await queue.enqueue(run_id)
    await queue.dequeue(owner="worker-1")
    # In-flight run must not be re-enqueued (idempotency covers the claim window).
    await queue.enqueue(run_id)
    assert await queue.pending_ids() == []


@pytest.mark.asyncio
async def test_ack_wrong_owner_is_noop():
    queue = InMemoryQueue()
    run_id = uuid.uuid4()
    await queue.enqueue(run_id)
    await queue.dequeue(owner="worker-1")
    await queue.ack(run_id, owner="wrong-owner")
    # The run stays in-flight; re-enqueue still blocked.
    await queue.enqueue(run_id)
    assert await queue.pending_ids() == []


# --------------------------------------------------------------------------- #
# Idempotent enqueue with a live lease (DB-backed)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_enqueue_skips_run_with_live_lease(db_session):
    """enqueue must consult live leases: a run with a non-expired lease is
    not re-enqueued even if it has no pending queue entry."""
    from tests.conftest import TestSessionLocal

    run = await _make_run(db_session)
    leases = LeaseStore(db_session)
    await leases.acquire(run.id, owner="worker-x", ttl_seconds=300)
    await db_session.commit()

    queue = InMemoryQueue()
    await queue.enqueue(run.id, db_session_factory=TestSessionLocal)
    assert await queue.pending_ids() == []


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_run_queue_returns_inmemory_by_default():
    """Without Redis the factory must degrade to InMemoryQueue (never crash)."""
    from app.agents.workflow.queue import set_run_queue
    set_run_queue(None)  # reset singleton
    queue = await get_run_queue()
    assert isinstance(queue, InMemoryQueue)
    set_run_queue(None)


# --------------------------------------------------------------------------- #
# Worker executes + acks
# --------------------------------------------------------------------------- #
async def _echo_executor(run_id, session):
    """Test double: emits two events and terminates."""
    yield AgentEvent(kind="step.started", data={"run_id": str(run_id)})
    yield AgentEvent(kind="run.completed", data={"status": "completed"})


@pytest.mark.asyncio
async def test_worker_executes_and_acks(db_session):
    """The worker claims a queued run, executes it, persists events, and acks."""
    from tests.conftest import TestSessionLocal

    run = await _make_run(db_session, status="pending")
    await db_session.commit()

    queue = InMemoryQueue()
    await queue.enqueue(run.id)
    worker = RunWorker(
        queue=queue,
        execute_fn=_echo_executor,
        session_factory=TestSessionLocal,
    )
    processed = await worker.run_once()
    assert processed == run.id

    # Queue is empty (acked).
    assert await queue.pending_ids() == []

    # Events were persisted.
    async with TestSessionLocal() as s:
        events = await EventStore(s).replay(run.id)
        kinds = [e.event_type for e in events]
        assert "run.started" in kinds
        assert "step.started" in kinds
        assert "run.completed" in kinds

        # Run status finalized.
        finished = await s.get(AgentRun, run.id)
        assert finished.status == "completed"

        # Lease released.
        leases = LeaseStore(s)
        from app.models import RunLease
        from sqlalchemy import select
        row = (
            await s.execute(select(RunLease).where(RunLease.run_id == run.id))
        ).scalar_one_or_none()
        assert row is None


@pytest.mark.asyncio
async def test_worker_transient_failure_marks_failed(db_session):
    """A transient exception in the executor marks the run failed + acks."""
    from tests.conftest import TestSessionLocal

    run = await _make_run(db_session, status="pending")
    await db_session.commit()

    async def failing_executor(run_id, session):
        yield AgentEvent(kind="step.started", data={})
        raise RuntimeError("transient boom")

    queue = InMemoryQueue()
    await queue.enqueue(run.id)
    worker = RunWorker(
        queue=queue,
        execute_fn=failing_executor,
        session_factory=TestSessionLocal,
    )
    processed = await worker.run_once()
    assert processed == run.id

    async with TestSessionLocal() as s:
        failed = await s.get(AgentRun, run.id)
        assert failed.status == "failed"
        assert "transient boom" in (failed.error_message or "")
    # Queue is empty (acked, not requeued by the worker).
    assert await queue.pending_ids() == []


@pytest.mark.asyncio
async def test_worker_skips_terminal_run(db_session):
    """A run that was cancelled while queued is acked without execution."""
    from tests.conftest import TestSessionLocal

    run = await _make_run(db_session, status="cancelled")
    await db_session.commit()

    queue = InMemoryQueue()
    await queue.enqueue(run.id)
    worker = RunWorker(
        queue=queue,
        execute_fn=_echo_executor,
        session_factory=TestSessionLocal,
    )
    processed = await worker.run_once()
    assert processed == run.id
    assert await queue.pending_ids() == []

    # No events were appended.
    async with TestSessionLocal() as s:
        events = await EventStore(s).replay(run.id)
        assert events == []
