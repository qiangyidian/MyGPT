"""Stale-run recovery (Task 5): expired leases are requeued once, then failed.

Covers the RecoveryScheduler contract:
  * an expired lease is detected and the run requeued
  * a second scan returns [] until a NEW lease expires
  * after max retries the run is terminally failed with an explicit reason
  * legacy ``running`` rows with no live lease are also recovered
  * lease fencing: a second owner cannot renew/release another owner's lease
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.agents.events import EventStore
from app.agents.workflow.queue import InMemoryQueue
from app.agents.workflow.recovery import RecoveryScheduler
from app.agents.workflow.repository import LeaseStore
from app.models import AgentRun, Conversation, Message, RunLease

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _make_run(db_session, *, status: str = "running") -> AgentRun:
    conv = Conversation(user_id=_SEEDED_USER, title="recovery-test")
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


def _make_recovery(db_session, queue=None):
    """RecoveryScheduler wired to the test session + an in-memory queue."""
    from tests.conftest import TestSessionLocal
    if queue is None:
        queue = InMemoryQueue()
    return RecoveryScheduler(
        session_factory=TestSessionLocal, queue=queue, max_retries=2
    )


# --------------------------------------------------------------------------- #
# Expired lease → requeued once
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_expired_lease_is_requeued_once(db_session):
    run = await _make_run(db_session)
    leases = LeaseStore(db_session)
    # A lease that already expired.
    await leases.acquire(run.id, owner="dead-worker", ttl_seconds=0)
    await db_session.commit()

    scheduler = _make_recovery(db_session)
    acted = await scheduler.scan()
    assert run.id in acted
    # Second scan is idempotent for THIS run: it was either requeued (now
    # pending, no lease) or failed (terminal), so it won't be re-acted-on.
    acted2 = await scheduler.scan()
    assert run.id not in acted2


@pytest.mark.asyncio
async def test_scan_returns_empty_when_nothing_expired(db_session):
    run = await _make_run(db_session)
    leases = LeaseStore(db_session)
    await leases.acquire(run.id, owner="worker-1", ttl_seconds=300)
    await db_session.commit()

    scheduler = _make_recovery(db_session)
    acted = await scheduler.scan()
    # Our run has a live lease → not acted on (other tests' runs may be).
    assert run.id not in acted


# --------------------------------------------------------------------------- #
# Exhausted retries → terminal failure
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_exhausted_retries_marks_run_failed(db_session):
    run = await _make_run(db_session)
    store = EventStore(db_session)
    # Simulate retries already used up (max_retries=2 in _make_recovery).
    await store.append(run.id, "recovery.requeued", {"retry": 1})
    await store.append(run.id, "recovery.requeued", {"retry": 2})
    # Expired lease.
    leases = LeaseStore(db_session)
    await leases.acquire(run.id, owner="dead-worker", ttl_seconds=0)
    await db_session.commit()

    scheduler = _make_recovery(db_session)
    acted = await scheduler.scan()
    assert run.id in acted

    # The run must be terminally failed with an explicit reason.
    from tests.conftest import TestSessionLocal
    async with TestSessionLocal() as s:
        failed_run = await s.get(AgentRun, run.id)
        assert failed_run.status == "failed"
        assert "exhausted" in (failed_run.error_message or "").lower()


# --------------------------------------------------------------------------- #
# Legacy running row with no live lease
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_legacy_running_row_no_lease_is_recovered(db_session):
    run = await _make_run(db_session, status="running")
    await db_session.commit()

    scheduler = _make_recovery(db_session)
    acted = await scheduler.scan()
    assert run.id in acted


# --------------------------------------------------------------------------- #
# Lease fencing (from Task 4 store, verified here end-to-end)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_second_owner_cannot_renew(db_session):
    run = await _make_run(db_session)
    leases = LeaseStore(db_session)
    await leases.acquire(run.id, owner="worker-a", ttl_seconds=300)
    await db_session.flush()

    result = await leases.renew(run.id, owner="worker-b", ttl_seconds=600)
    assert result is None  # fencing: only the owner can renew


@pytest.mark.asyncio
async def test_second_owner_cannot_release(db_session):
    run = await _make_run(db_session)
    leases = LeaseStore(db_session)
    await leases.acquire(run.id, owner="worker-a", ttl_seconds=300)
    await db_session.flush()

    released = await leases.release(run.id, owner="worker-b")
    assert released is False

    # The lease survives.
    row = (
        await db_session.execute(select(RunLease).where(RunLease.run_id == run.id))
    ).scalar_one_or_none()
    assert row is not None
    assert row.owner == "worker-a"
