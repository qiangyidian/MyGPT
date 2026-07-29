"""Approval bus tests.

The Redis pub/sub path needs a live Redis; the test env has none, so these
verify the *in-process fallback* (which is exactly what runs in single-worker
prod and in tests): ``publish`` signals the local coordinator even when Redis
is unavailable. The same code path is what a remote worker's subscriber would
invoke locally upon receiving a Redis message.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.agents.approval_bus import ApprovalBus, approval_bus
from app.agents.approval_coordinator import ApprovalCoordinator, approval_coordinator


async def test_publish_signals_local_coordinator_without_redis():
    """publish() must wake a locally-waiting run even when Redis is down."""
    coord = approval_coordinator  # the singleton the bus signals
    run_id = uuid.uuid4()
    ap_id = uuid.uuid4()
    coord.register(run_id=run_id, approval_id=ap_id, tool_name="db_query")

    # Force the bus to treat Redis as unavailable (it is, in tests).
    bus = ApprovalBus()
    bus._redis_ok = False

    async def approve_after_pause():
        await asyncio.sleep(0.05)
        await bus.publish(approval_id=str(ap_id), decision="approved")

    asyncio.create_task(approve_after_pause())
    wr = await coord.wait(ap_id, timeout=3)
    assert wr.decision == "approved"


async def test_publish_reject_signals_local():
    coord = approval_coordinator
    run_id = uuid.uuid4()
    ap_id = uuid.uuid4()
    coord.register(run_id=run_id, approval_id=ap_id, tool_name="db_query")

    bus = ApprovalBus()
    bus._redis_ok = False

    async def reject_after_pause():
        await asyncio.sleep(0.05)
        await bus.publish(approval_id=str(ap_id), decision="rejected", reason="nope")

    asyncio.create_task(reject_after_pause())
    wr = await coord.wait(ap_id, timeout=3)
    assert wr.decision == "rejected"
    assert wr.reason == "nope"


async def test_redis_unavailable_probe_caches_and_does_not_raise():
    """_redis_available() must never raise and must cache its result."""
    bus = ApprovalBus()
    bus._redis_ok = None  # force a probe
    ok = await bus._redis_available()
    assert ok is False  # no Redis in the test env
    # Cached: a second call doesn't re-probe.
    assert bus._redis_ok is False


async def test_start_subscriber_noop_without_redis():
    """start_subscriber is a no-op (no task spawned) when Redis is unavailable."""
    bus = ApprovalBus()
    bus._redis_ok = False
    bus._started = False
    await bus.start_subscriber()
    assert bus._sub_task is None
    await bus.stop()
