"""Conversation CRUD + cross-user ownership isolation."""
from __future__ import annotations

import uuid

from app.models import Message
from tests.conftest import auth_headers


async def test_conversation_crud(client):
    h = auth_headers()
    created = await client.post("/api/conversations", json={"title": "test conv"}, headers=h)
    assert created.status_code == 201
    cid = created.json()["id"]

    listed = await client.get("/api/conversations", headers=h)
    assert listed.status_code == 200
    assert any(c["id"] == cid for c in listed.json())

    detail = await client.get(f"/api/conversations/{cid}", headers=h)
    assert detail.status_code == 200
    assert detail.json()["id"] == cid

    deleted = await client.delete(f"/api/conversations/{cid}", headers=h)
    assert deleted.status_code == 204


async def test_cross_user_isolation_returns_404(client):
    # Conversation owned by the seeded user.
    h1 = auth_headers()
    created = await client.post("/api/conversations", json={"title": "mine"}, headers=h1)
    cid = created.json()["id"]

    # A different registered user must not see it.
    reg = await client.post(
        "/api/auth/register",
        json={"email": "snooper@example.com", "username": "snooper", "password": "Passw0rd!"},
    )
    h2 = {"Authorization": f"Bearer {reg.json()['access_token']}"}

    foreign = await client.get(f"/api/conversations/{cid}", headers=h2)
    assert foreign.status_code == 404


async def test_messages_ordered_user_before_assistant_in_same_transaction(
    client, db_session
):
    """A turn persists the user message + assistant placeholder in ONE commit.

    They must come back oldest-first (user THEN assistant) even though they
    share a transaction. Regression guard for the created_at-tie ordering bug:
    server_default=func.now() resolves to the transaction-start time (identical
    for both rows), which made ORDER BY created_at non-deterministic and let the
    assistant reply sort ahead of the user's question. The Message model now
    uses a per-row Python timestamp so the two rows are distinct and ordered.
    """
    h = auth_headers()
    created = await client.post(
        "/api/conversations", json={"title": "order-test"}, headers=h
    )
    cid = created.json()["id"]
    conv_id = uuid.UUID(cid)

    # Insert user + assistant in a SINGLE commit (mirrors ChatService._run).
    db_session.add(Message(conversation_id=conv_id, role="user", content="Q"))
    db_session.add(Message(conversation_id=conv_id, role="assistant", content="A"))
    await db_session.commit()

    detail = await client.get(f"/api/conversations/{cid}", headers=h)
    assert detail.status_code == 200
    msgs = detail.json()["messages"]
    assert len(msgs) == 2
    # Oldest-first: the user's question precedes the assistant's reply.
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    # The fix: the two rows get DISTINCT created_at (not the shared
    # transaction-start time), so the order is deterministic.
    assert msgs[0]["created_at"] <= msgs[1]["created_at"]
    assert msgs[0]["created_at"] != msgs[1]["created_at"]
