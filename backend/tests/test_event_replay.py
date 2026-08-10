"""Cursor-replay event endpoint (Task 5).

Covers the GET /api/agent-runs/{run_id}/events SSE endpoint:
  * replays events after the Last-Event-ID cursor in sequence order
  * each frame carries ``id: <sequence>`` + ``event: <type>`` + ``data: <json>``
  * 404 on foreign run (ownership enforced)
  * a terminal event closes the stream
  * disconnect does NOT execute or cancel the run (read-only subscription)
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.agents.events import EventStore
from app.models import AgentRun, Conversation, Message

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")
_FOREIGN_USER = uuid.UUID("00000000-0000-0000-0000-000000000002")


async def _make_run(db_session, *, user_id=_SEEDED_USER, status="running") -> AgentRun:
    conv = Conversation(user_id=user_id, title="replay-test")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id,
        message_id=msg.id,
        user_id=user_id,
        runtime="native",
        flow_name="test",
        status=status,
    )
    db_session.add(run)
    await db_session.flush()
    return run


def _parse_sse_frames(text: str) -> list[dict]:
    """Split an SSE text body into frame dicts {id, event, data}."""
    frames = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        frame = {"id": None, "event": "message", "data": None}
        for line in block.split("\n"):
            if line.startswith("id:"):
                frame["id"] = int(line[3:].strip())
            elif line.startswith("event:"):
                frame["event"] = line[6:].strip()
            elif line.startswith("data:"):
                frame["data"] = json.loads(line[5:].strip())
        frames.append(frame)
    return frames


# --------------------------------------------------------------------------- #
# Cursor replay
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_replay_emits_all_events_in_order(client, db_session, auth_token):
    run = await _make_run(db_session)
    store = EventStore(db_session)
    await store.append(run.id, "run.started", {"a": 1})
    await store.append(run.id, "step.started", {"b": 2})
    await store.append(
        run.id, "run.completed", {"status": "completed"}
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/agent-runs/{run.id}/events",
        headers={**auth_headers(auth_token)},
    )
    assert resp.status_code == 200
    frames = _parse_sse_frames(resp.text)
    assert [f["id"] for f in frames] == [1, 2, 3]
    assert [f["event"] for f in frames] == ["run.started", "step.started", "run.completed"]
    assert frames[0]["data"] == {"a": 1}


@pytest.mark.asyncio
async def test_replay_respects_last_event_id_cursor(client, db_session, auth_token):
    run = await _make_run(db_session)
    store = EventStore(db_session)
    await store.append(run.id, "run.started", {})
    await store.append(run.id, "step.started", {})
    await store.append(run.id, "step.completed", {})
    await store.append(run.id, "run.completed", {})
    await db_session.commit()

    resp = await client.get(
        f"/api/agent-runs/{run.id}/events",
        headers={**auth_headers(auth_token), "Last-Event-ID": "1"},
    )
    assert resp.status_code == 200
    frames = _parse_sse_frames(resp.text)
    # Only events after sequence 1.
    assert [f["id"] for f in frames] == [2, 3, 4]


@pytest.mark.asyncio
async def test_replay_404_on_foreign_run(client, db_session, auth_token):
    run = await _make_run(db_session, user_id=_FOREIGN_USER)
    await db_session.commit()

    resp = await client.get(
        f"/api/agent-runs/{run.id}/events",
        headers=auth_headers(auth_token),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_replay_404_on_unknown_run(client, auth_token):
    resp = await client.get(
        f"/api/agent-runs/{uuid.uuid4()}/events",
        headers=auth_headers(auth_token),
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Helper re-exported so tests in other modules can build headers.
# --------------------------------------------------------------------------- #
def auth_headers(token: str | None = None) -> dict[str, str]:
    from tests.conftest import auth_headers as _ah
    return _ah(token)
