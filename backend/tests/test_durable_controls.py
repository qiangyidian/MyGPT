"""Durable command store + lease lifecycle (Task 4).

Covers:
  * CommandStore: append (pending), exactly-once claim_pending, mark_applied /
    mark_failed transitions, per-run scoping.
  * LeaseStore: acquire (creates + bumps version), renew (owner-checked),
    release (owner-checked), is_expired.
  * The high-level controls.* writers used by the API layer.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.agents.workflow import controls as control_writers
from app.agents.workflow.repository import CommandStore, LeaseStore
from app.models import AgentRun, Conversation, Message, RunCommand

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _make_run(db_session) -> AgentRun:
    conv = Conversation(user_id=_SEEDED_USER, title="controls")
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
        status="running",
    )
    db_session.add(run)
    await db_session.flush()
    return run


# --------------------------------------------------------------------------- #
# CommandStore
# --------------------------------------------------------------------------- #
async def test_instruction_is_claimed_once(db_session):
    run = await _make_run(db_session)
    commands = CommandStore(db_session)
    command = await commands.append(run.id, "instruction", {"text": "check sources"})
    assert await commands.claim_pending(run.id) == [command]
    assert await commands.claim_pending(run.id) == []


async def test_command_append_starts_pending(db_session):
    run = await _make_run(db_session)
    commands = CommandStore(db_session)
    cmd = await commands.append(run.id, "pause", {})
    assert cmd.status == "pending"
    assert cmd.command_type == "pause"
    assert cmd.payload == {}


async def test_claim_sets_claimed_fields(db_session):
    run = await _make_run(db_session)
    commands = CommandStore(db_session)
    await commands.append(run.id, "cancel", {})
    claimed = await commands.claim_pending(run.id, owner="worker-1")
    assert len(claimed) == 1
    assert claimed[0].status == "claimed"
    assert claimed[0].claimed_by == "worker-1"
    assert claimed[0].claimed_at is not None


async def test_mark_applied_and_failed(db_session):
    run = await _make_run(db_session)
    commands = CommandStore(db_session)
    cmd = await commands.append(run.id, "resume", {})
    await commands.claim_pending(run.id)
    assert await commands.mark_applied(cmd.id) is True
    assert cmd.status == "applied"
    assert cmd.applied_at is not None

    cmd2 = await commands.append(run.id, "pause", {})
    await commands.claim_pending(run.id)
    assert await commands.mark_failed(cmd2.id, "boom") is True
    assert cmd2.status == "failed"
    assert cmd2.error == "boom"


async def test_claim_scoped_to_run(db_session):
    run_a = await _make_run(db_session)
    run_b = await _make_run(db_session)
    commands = CommandStore(db_session)
    await commands.append(run_a.id, "pause", {})
    await commands.append(run_b.id, "cancel", {})
    a_claimed = await commands.claim_pending(run_a.id)
    b_claimed = await commands.claim_pending(run_b.id)
    assert {c.command_type for c in a_claimed} == {"pause"}
    assert {c.command_type for c in b_claimed} == {"cancel"}


async def test_claim_does_not_reclaim_applied(db_session):
    run = await _make_run(db_session)
    commands = CommandStore(db_session)
    cmd = await commands.append(run.id, "pause", {})
    await commands.claim_pending(run.id)
    await commands.mark_applied(cmd.id)
    # Already-applied commands are never re-claimed.
    assert await commands.claim_pending(run.id) == []


async def test_mark_transitions_only_apply_to_claimed_commands(db_session):
    """mark_applied/mark_failed guard on status=='claimed' and report the flip.

    A pending command cannot skip the claim step, and mark_failed must never
    overwrite an applied command.
    """
    run = await _make_run(db_session)
    commands = CommandStore(db_session)
    cmd = await commands.append(run.id, "pause", {})
    # Pending -> applied is rejected (must be claimed first).
    assert await commands.mark_applied(cmd.id) is False
    assert cmd.status == "pending"
    assert await commands.mark_failed(cmd.id, "early") is False
    assert cmd.status == "pending"

    # Claim, then applied succeeds.
    await commands.claim_pending(run.id)
    assert await commands.mark_applied(cmd.id) is True
    assert cmd.status == "applied"
    # mark_failed must NOT overwrite an applied command.
    assert await commands.mark_failed(cmd.id, "late") is False
    assert cmd.status == "applied"
    assert cmd.error is None

    # Unknown command id -> False, no error.
    assert await commands.mark_applied(uuid.uuid4()) is False
    assert await commands.mark_failed(uuid.uuid4(), "x") is False


# --------------------------------------------------------------------------- #
# High-level controls writers (used by the persist-first API layer)
# --------------------------------------------------------------------------- #
async def test_control_writers_persist_commands(db_session):
    run = await _make_run(db_session)
    await control_writers.record_pause(db_session, run.id)
    await control_writers.record_resume(db_session, run.id)
    await control_writers.record_cancel(db_session, run.id)
    await control_writers.record_instruction(db_session, run.id, "be concise")
    approval_id = uuid.uuid4()
    await control_writers.record_approve(
        db_session, run.id, approval_id, user_id=_SEEDED_USER
    )
    await control_writers.record_reject(
        db_session, run.id, uuid.uuid4(), reason="risky", user_id=_SEEDED_USER
    )
    await db_session.flush()

    from sqlalchemy import select

    rows = (
        await db_session.execute(
            select(RunCommand).where(RunCommand.run_id == run.id)
        )
    ).scalars().all()
    # All six commands persisted as pending (order is not asserted — on the
    # SQLite test DB created_at has second granularity and UUID ids are not
    # insertion-ordered, so only the set + payloads are deterministic).
    assert {r.command_type for r in rows} == {
        "pause",
        "resume",
        "cancel",
        "instruction",
        "approve",
        "reject",
    }
    assert all(r.status == "pending" for r in rows)
    instr = next(r for r in rows if r.command_type == "instruction")
    assert instr.payload == {"text": "be concise"}
    approve = next(r for r in rows if r.command_type == "approve")
    assert approve.payload == {
        "approval_id": str(approval_id),
        "user_id": str(_SEEDED_USER),
    }
    reject = next(r for r in rows if r.command_type == "reject")
    assert reject.payload["reason"] == "risky"
    assert reject.payload["user_id"] == str(_SEEDED_USER)


# --------------------------------------------------------------------------- #
# LeaseStore
# --------------------------------------------------------------------------- #
async def test_lease_acquire_creates_lease(db_session):
    run = await _make_run(db_session)
    leases = LeaseStore(db_session)
    lease = await leases.acquire(run.id, owner="w1", ttl_seconds=60)
    assert lease.owner == "w1"
    assert lease.version == 1
    assert lease.expires_at is not None
    assert lease.acquired_at is not None


async def test_lease_reacquire_bumps_version(db_session):
    run = await _make_run(db_session)
    leases = LeaseStore(db_session)
    first = await leases.acquire(run.id, owner="w1", ttl_seconds=60)
    first_version = first.version
    second = await leases.acquire(run.id, owner="w1", ttl_seconds=60)
    assert second.version == first_version + 1


async def test_lease_acquire_by_new_owner_after_expire(db_session):
    run = await _make_run(db_session)
    leases = LeaseStore(db_session)
    await leases.acquire(run.id, owner="w1", ttl_seconds=60)
    # A different owner may take over (e.g. after expiry) — acquire overwrites.
    second = await leases.acquire(run.id, owner="w2", ttl_seconds=60)
    assert second.owner == "w2"
    assert second.version == 2


async def test_lease_renew_extends_expiry(db_session):
    run = await _make_run(db_session)
    leases = LeaseStore(db_session)
    await leases.acquire(run.id, owner="w1", ttl_seconds=60)
    renewed = await leases.renew(run.id, owner="w1", ttl_seconds=120)
    assert renewed is not None
    assert renewed.version >= 2


async def test_lease_renew_wrong_owner_returns_none(db_session):
    run = await _make_run(db_session)
    leases = LeaseStore(db_session)
    await leases.acquire(run.id, owner="w1", ttl_seconds=60)
    assert await leases.renew(run.id, owner="w2", ttl_seconds=60) is None


async def test_lease_renew_missing_returns_none(db_session):
    run = await _make_run(db_session)
    leases = LeaseStore(db_session)
    assert await leases.renew(run.id, owner="w1", ttl_seconds=60) is None


async def test_lease_release(db_session):
    run = await _make_run(db_session)
    leases = LeaseStore(db_session)
    await leases.acquire(run.id, owner="w1", ttl_seconds=60)
    assert await leases.release(run.id, owner="w1") is True
    # Second release is a no-op (lease already gone).
    assert await leases.release(run.id, owner="w1") is False


async def test_lease_release_wrong_owner(db_session):
    run = await _make_run(db_session)
    leases = LeaseStore(db_session)
    await leases.acquire(run.id, owner="w1", ttl_seconds=60)
    assert await leases.release(run.id, owner="w2") is False


async def test_lease_is_expired(db_session):
    run = await _make_run(db_session)
    leases = LeaseStore(db_session)
    lease = await leases.acquire(run.id, owner="w1", ttl_seconds=60)
    # Default now is within the 60s TTL -> not expired.
    assert leases.is_expired(lease) is False
    # A moment later than expiry -> expired.
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert leases.is_expired(lease, now=future) is True


# --------------------------------------------------------------------------- #
# API endpoint: persist-first ordering (regression guard)
# --------------------------------------------------------------------------- #
async def test_cancel_endpoint_persists_command_before_signal(client, db_session):
    """A control endpoint must PERSIST a RunCommand before flipping the run.

    Locks the persist-first ordering in as a regression guard: the durable
    command row exists even when there is no live run to signal.
    """
    from sqlalchemy import select as _sel

    from tests.conftest import auth_headers

    conv = Conversation(user_id=_SEEDED_USER, title="durable cancel")
    db_session.add(conv)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id,
        user_id=_SEEDED_USER,
        runtime="native",
        flow_name="test",
        status="running",
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)

    h = auth_headers()
    r = await client.post(f"/api/agent-runs/{run.id}/cancel", headers=h)
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # The durable cancel command was committed (persist-first).
    cmds = (
        await db_session.execute(
            _sel(RunCommand).where(RunCommand.run_id == run.id)
        )
    ).scalars().all()
    assert len(cmds) == 1
    assert cmds[0].command_type == "cancel"
    assert cmds[0].status == "pending"
    # And the run status flipped to cancelled.
    status = (
        await db_session.execute(
            _sel(AgentRun.status).where(AgentRun.id == run.id)
        )
    ).scalar_one()
    assert status == "cancelled"


async def test_pause_endpoint_persists_command(client, db_session):
    """The pause endpoint also writes a durable command (persist-first)."""
    from sqlalchemy import select as _sel

    from tests.conftest import auth_headers

    conv = Conversation(user_id=_SEEDED_USER, title="durable pause")
    db_session.add(conv)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id,
        user_id=_SEEDED_USER,
        runtime="native",
        flow_name="test",
        status="running",
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)

    h = auth_headers()
    r = await client.post(f"/api/agent-runs/{run.id}/pause", headers=h)
    assert r.status_code == 200

    cmds = (
        await db_session.execute(
            _sel(RunCommand).where(RunCommand.run_id == run.id)
        )
    ).scalars().all()
    assert len(cmds) == 1
    assert cmds[0].command_type == "pause"
    assert cmds[0].status == "pending"
