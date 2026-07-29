"""Message feedback (thumbs up/down): upsert, retrieve, delete, ownership."""
from __future__ import annotations

import uuid

from app.models import Conversation, Message, User
from tests.conftest import auth_headers

_SEEDED = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _seed_assistant_message(db_session) -> uuid.UUID:
    conv = Conversation(user_id=_SEEDED, title="feedback test")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="hi", metadata_={})
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)
    return msg.id


async def test_feedback_upsert_and_get(client, db_session):
    mid = await _seed_assistant_message(db_session)
    h = auth_headers()

    r = await client.post(
        f"/api/messages/{mid}/feedback",
        json={"rating": "up", "reason": "helpful"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["rating"] == "up"

    # Flip to down (upsert — same user+message row).
    r2 = await client.post(
        f"/api/messages/{mid}/feedback",
        json={"rating": "down", "comment": "actually wrong"},
        headers=h,
    )
    assert r2.status_code == 200
    assert r2.json()["rating"] == "down"

    g = await client.get(f"/api/messages/{mid}/feedback", headers=h)
    assert g.status_code == 200
    assert g.json()["rating"] == "down"


async def test_feedback_delete(client, db_session):
    mid = await _seed_assistant_message(db_session)
    h = auth_headers()
    await client.post(f"/api/messages/{mid}/feedback", json={"rating": "up"}, headers=h)
    d = await client.delete(f"/api/messages/{mid}/feedback", headers=h)
    assert d.status_code == 204
    g = await client.get(f"/api/messages/{mid}/feedback", headers=h)
    assert g.status_code == 200
    assert g.json() is None


async def test_feedback_invalid_rating_rejected(client, db_session):
    mid = await _seed_assistant_message(db_session)
    h = auth_headers()
    r = await client.post(
        f"/api/messages/{mid}/feedback", json={"rating": "sideways"}, headers=h
    )
    assert r.status_code in (400, 422)


async def test_feedback_cross_user_isolation(client, db_session):
    mid = await _seed_assistant_message(db_session)
    reg = await client.post(
        "/api/auth/register",
        json={"email": "fb-other@example.com", "username": "fb-other", "password": "Passw0rd!"},
    )
    h2 = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    # Another user cannot feedback/access a message they don't own -> 404.
    r = await client.post(f"/api/messages/{mid}/feedback", json={"rating": "up"}, headers=h2)
    assert r.status_code == 404
