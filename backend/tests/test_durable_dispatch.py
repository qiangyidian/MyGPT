"""Durable dispatch integration tests (Task 5 review C1 + I1 + M1).

C1 — With ``BACKGROUND_WORKER != "inprocess"``, POST /api/chat/stream creates
      the turn records, enqueues the run, and returns an SSE stream that tails
      the durable event log. The inline executor is NOT invoked.

I1 — Lease loss mid-execution aborts the disowned worker (no split-brain): the
      worker stops appending events + finalizing and exits WITHOUT acking.

M1 — A client disconnecting from the events stream does NOT cancel the run —
      the worker keeps executing and the run reaches terminal ``completed``.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.agents.events import EventStore
from app.agents.workflow.execution import execute_run
from app.agents.workflow.queue import InMemoryQueue, set_run_queue
from app.agents.workflow.repository import LeaseStore
from app.agents.workflow.worker import RunWorker
from app.core.config import get_settings
from app.models import AgentRun, Conversation, Message, RunLease
from tests.conftest import TestSessionLocal, auth_headers

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _create_mock_model(client, headers):
    r = await client.post(
        "/api/models",
        json={
            "name": "Durable dispatch mock",
            "provider": "openai-compatible",
            "api_base_url": "http://localhost/v1",
            "model_name": "mock-model",
            "supports_stream": True,
            "supports_tools": True,
            "is_embedding": False,
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --------------------------------------------------------------------------- #
# C1: durable dispatch wiring
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_durable_dispatch_creates_and_enqueues(client, db_session, monkeypatch, offline_model):
    """BACKGROUND_WORKER=durable → POST /chat/stream creates a run + enqueues,
    does NOT execute inline, and the stream reaches terminal after the worker
    processes the run."""
    # Force durable mode.
    settings = get_settings()
    monkeypatch.setattr(settings, "BACKGROUND_WORKER", "durable")

    # Bind the chat_service persistence factory to the test DB (same as the
    # client fixture does, but we need it here because we also drive the worker).
    from app.services.chat_service import chat_service

    original_factory = chat_service._persistence_session_factory
    chat_service._persistence_session_factory = TestSessionLocal

    # Inject an in-memory queue so we can observe the enqueue + drive the worker.
    queue = InMemoryQueue()
    set_run_queue(queue)
    # Also reset the queue singleton the chat_service reads via get_run_queue().
    # set_run_queue sets the module global; get_run_queue returns it.

    h = auth_headers()
    model_id = await _create_mock_model(client, h)

    try:
        # Count existing runs so we can assert exactly ONE new run is created.
        pre_count = len(
            (await db_session.execute(select(AgentRun))).scalars().all()
        )

        # POST /chat/stream in durable mode. The handler creates the run +
        # enqueues, then returns a StreamingResponse tailing the events.
        # We read the stream concurrently while driving the worker.
        events_seen: list[str] = []

        async def _drive_worker():
            # Give the dispatch a moment to create + enqueue the run.
            await asyncio.sleep(0.2)
            worker = RunWorker(
                queue=queue,
                execute_fn=execute_run,
                session_factory=TestSessionLocal,
            )
            await worker.run_once()

        worker_task = asyncio.create_task(_drive_worker())

        async with client.stream(
            "POST",
            "/api/chat/stream",
            json={"content": "hello durable dispatch", "model_id": model_id},
            headers=h,
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers.get("x-durable-run-id") is not None
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    events_seen.append(line.split(":", 1)[1].strip())
                # The stream terminates after a terminal event frame.

        await worker_task

        # The run was enqueued (and consumed by our worker).
        assert await queue.pending_ids() == []

        # Exactly ONE new AgentRun was created (no inline duplicate).
        post_runs = (await db_session.execute(select(AgentRun))).scalars().all()
        assert len(post_runs) == pre_count + 1, (
            f"expected exactly 1 new run, got {len(post_runs) - pre_count}"
        )

        # The run reached terminal completed.
        new_run = post_runs[-1]
        await db_session.refresh(new_run)
        assert new_run.status == "completed", (
            f"run status={new_run.status} err={new_run.error_message}"
        )

        # The client saw at least meta + a terminal event.
        assert "meta" in events_seen, f"no meta event in {events_seen}"
        assert events_seen[-1] in (
            "done", "error", "run.completed", "run.failed"
        ), f"unexpected last event {events_seen[-1]} in {events_seen}"
    finally:
        chat_service._persistence_session_factory = original_factory
        set_run_queue(None)


@pytest.mark.asyncio
async def test_inline_mode_unchanged_by_dispatch(client, db_session, monkeypatch):
    """BACKGROUND_WORKER=inprocess (default) → existing inline path, no enqueue."""
    settings = get_settings()
    monkeypatch.setattr(settings, "BACKGROUND_WORKER", "inprocess")

    queue = InMemoryQueue()
    set_run_queue(queue)

    h = auth_headers()
    model_id = await _create_mock_model(client, h)

    events_seen: list[str] = []
    async with client.stream(
        "POST",
        "/api/chat/stream",
        json={"content": "inline still works", "model_id": model_id},
        headers=h,
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers.get("x-durable-run-id") is None
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                events_seen.append(line.split(":", 1)[1].strip())

    # Inline path was used: no durable enqueue.
    assert await queue.pending_ids() == []
    assert "meta" in events_seen
    assert events_seen[-1] in ("done", "error")
    set_run_queue(None)


# --------------------------------------------------------------------------- #
# I1: lease loss aborts the disowned worker (no split-brain)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_lease_loss_aborts_execution_without_acking(db_session):
    """When the renewal loop detects lease loss, the worker stops appending
    events, does NOT finalize, and does NOT ack — recovery owns the run."""
    from app.models import AgentRun, Conversation, Message

    conv = Conversation(user_id=_SEEDED_USER, title="lease-loss-test")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id, message_id=msg.id, user_id=_SEEDED_USER,
        runtime="native", flow_name="test", status="pending",
    )
    db_session.add(run)
    await db_session.commit()

    queue = InMemoryQueue()
    await queue.enqueue(run.id)

    started = asyncio.Event()
    block = asyncio.Event()

    async def slow_executor(rid, session):
        yield type("E", (), {"kind": "step.started", "data": {}})()
        started.set()
        await block.wait()  # hold until the test triggers lease loss
        yield type("E", (), {"kind": "run.completed", "data": {}})()

    # Custom settings with a very short renewal interval so the renewal loop
    # detects the stolen lease quickly.
    from types import SimpleNamespace

    fast_settings = SimpleNamespace(
        RUN_LEASE_TTL_SECONDS=300,
        RUN_LEASE_RENEW_SECONDS=0,  # check immediately
        WORKER_POLL_INTERVAL_SECONDS=0.5,
        WORKER_BLOCK_TIMEOUT_SECONDS=0,
    )

    worker = RunWorker(
        queue=queue,
        execute_fn=slow_executor,
        session_factory=TestSessionLocal,
        owner="worker-a",
        settings=fast_settings,
    )

    process_task = asyncio.create_task(worker.run_once())
    await started.wait()

    # Simulate another worker stealing the lease.
    async with TestSessionLocal() as s:
        await LeaseStore(s).acquire(run.id, owner="worker-b", ttl_seconds=300)
        await s.commit()

    # Give the renewal loop time to wake up, detect the loss, and signal.
    await asyncio.sleep(0.3)

    # Release the executor's block so it yields the terminal event.
    block.set()
    await process_task

    # The queue was NOT acked — the run stays in-flight for recovery to requeue.
    assert run.id in queue._in_flight, (
        "worker acked after lease loss — should have left the run for recovery"
    )

    # The worker did NOT persist the terminal event (the lease-loss fence
    # stopped the loop before the run.completed was appended).
    async with TestSessionLocal() as s:
        events = await EventStore(s).replay(run.id)
        kinds = [e.event_type for e in events]
        assert "run.started" in kinds
        assert "run.completed" not in kinds, (
            f"worker persisted terminal event after lease loss: {kinds}"
        )


# --------------------------------------------------------------------------- #
# M1: disconnect-safety — client disconnect does NOT cancel the run
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_disconnect_does_not_cancel_run(client, db_session, monkeypatch, offline_model):
    """A client disconnecting from the events stream does NOT cancel the run.
    The worker keeps executing and the run reaches terminal ``completed``."""
    settings = get_settings()
    monkeypatch.setattr(settings, "BACKGROUND_WORKER", "durable")

    from app.services.chat_service import chat_service

    original_factory = chat_service._persistence_session_factory
    chat_service._persistence_session_factory = TestSessionLocal

    queue = InMemoryQueue()
    set_run_queue(queue)

    h = auth_headers()
    model_id = await _create_mock_model(client, h)

    try:
        captured_run_id: list[str] = []

        async def _drive_worker():
            await asyncio.sleep(0.2)
            worker = RunWorker(
                queue=queue,
                execute_fn=execute_run,
                session_factory=TestSessionLocal,
            )
            await worker.run_once()

        worker_task = asyncio.create_task(_drive_worker())

        # Open the stream then disconnect after reading the meta frame.
        async with client.stream(
            "POST",
            "/api/chat/stream",
            json={"content": "disconnect safety test", "model_id": model_id},
            headers=h,
        ) as resp:
            captured_run_id.append(resp.headers.get("x-durable-run-id", ""))
            # Read just the first frame (meta), then break (disconnect).
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    break  # client disconnects after the first event

        # The worker task is still running — let it finish.
        await worker_task

        run_id = uuid.UUID(captured_run_id[0])
        async with TestSessionLocal() as s:
            # The run STILL reached terminal completed despite the disconnect.
            finished = await s.get(AgentRun, run_id)
            assert finished is not None
            assert finished.status == "completed", (
                f"run not completed after disconnect: {finished.status}"
            )
            # The event log is intact (events were produced by the worker).
            events = await EventStore(s).replay(run_id)
            kinds = [e.event_type for e in events]
            assert len(events) > 1, f"event log empty after disconnect: {kinds}"
    finally:
        chat_service._persistence_session_factory = original_factory
        set_run_queue(None)
