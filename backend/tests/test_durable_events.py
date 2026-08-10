"""Durable workflow event store: monotonic per-run sequences + replay.

Covers the Task 4 EventStore contract:
  * sequences are monotonic per run, starting at 1
  * each run has its own independent sequence space
  * replay returns events ordered by sequence, optionally after a cursor
"""
from __future__ import annotations

import uuid

from app.agents.events import EventStore
from app.models import AgentRun, Conversation, Message

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _make_run(db_session) -> AgentRun:
    conv = Conversation(user_id=_SEEDED_USER, title="events")
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


async def test_run_events_get_monotonic_sequences(db_session):
    run = await _make_run(db_session)
    store = EventStore(db_session)
    first = await store.append(run.id, "run.started", {})
    second = await store.append(run.id, "step.started", {})
    assert (first.sequence, second.sequence) == (1, 2)


async def test_sequences_are_per_run(db_session):
    run_a = await _make_run(db_session)
    run_b = await _make_run(db_session)
    store = EventStore(db_session)
    a1 = await store.append(run_a.id, "run.started", {})
    b1 = await store.append(run_b.id, "run.started", {})
    a2 = await store.append(run_a.id, "step.started", {})
    assert (a1.sequence, b1.sequence, a2.sequence) == (1, 1, 2)


async def test_replay_returns_events_in_order(db_session):
    run = await _make_run(db_session)
    store = EventStore(db_session)
    await store.append(run.id, "run.started", {"a": 1})
    await store.append(run.id, "step.started", {"b": 2})
    await store.append(run.id, "run.completed", {"c": 3})
    events = await store.replay(run.id)
    assert [e.sequence for e in events] == [1, 2, 3]
    assert [e.event_type for e in events] == [
        "run.started",
        "step.started",
        "run.completed",
    ]


async def test_replay_after_sequence(db_session):
    run = await _make_run(db_session)
    store = EventStore(db_session)
    await store.append(run.id, "run.started", {})
    await store.append(run.id, "step.started", {})
    await store.append(run.id, "step.done", {})
    events = await store.replay(run.id, after_sequence=1)
    assert [e.sequence for e in events] == [2, 3]


async def test_replay_empty_for_unknown_run(db_session):
    store = EventStore(db_session)
    events = await store.replay(uuid.uuid4())
    assert events == []


async def test_event_data_defaults_to_empty_dict(db_session):
    run = await _make_run(db_session)
    store = EventStore(db_session)
    evt = await store.append(run.id, "run.started", {})
    assert evt.data == {}
    # Explicit payload is preserved untouched.
    evt2 = await store.append(run.id, "step.started", {"k": "v"})
    assert evt2.data == {"k": "v"}


async def test_event_persists_across_sessions(db_session):
    run = await _make_run(db_session)
    store = EventStore(db_session)
    await store.append(run.id, "run.started", {"x": 1})
    await db_session.commit()

    # A fresh store over the same shared in-memory DB replays the committed row.
    from tests.conftest import TestSessionLocal

    async with TestSessionLocal() as other:
        events = await EventStore(other).replay(run.id)
    assert len(events) == 1
    assert events[0].event_type == "run.started"
    assert events[0].data == {"x": 1}
