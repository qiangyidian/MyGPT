"""Conversation management: rename / pin / archive / search / branch."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, UTC

from app.models import Conversation, Message
from tests.conftest import auth_headers


async def test_rename(client):
    h = auth_headers()
    cid = (await client.post("/api/conversations", json={"title": "old"}, headers=h)).json()["id"]
    r = await client.patch(f"/api/conversations/{cid}", json={"title": "new name"}, headers=h)
    assert r.status_code == 200
    assert r.json()["title"] == "new name"


async def test_pin_and_pin_first_ordering(client):
    h = auth_headers()
    a = (await client.post("/api/conversations", json={"title": "alpha"}, headers=h)).json()["id"]
    b = (await client.post("/api/conversations", json={"title": "beta"}, headers=h)).json()["id"]
    # Pin the older one (alpha); it should float to the top regardless of time.
    assert (await client.patch(f"/api/conversations/{a}", json={"pinned": True}, headers=h)).status_code == 200
    listed = (await client.get("/api/conversations", headers=h)).json()
    assert listed[0]["id"] == a
    assert listed[0]["is_pinned"] is True
    assert any(c["id"] == b and c["is_pinned"] is False for c in listed)


async def test_archive_hides_from_default_list(client):
    h = auth_headers()
    cid = (await client.post("/api/conversations", json={"title": "to archive"}, headers=h)).json()["id"]
    await client.patch(f"/api/conversations/{cid}", json={"archived": True}, headers=h)
    active = (await client.get("/api/conversations", headers=h)).json()
    archived = (await client.get("/api/conversations?archived=true", headers=h)).json()
    assert all(c["id"] != cid for c in active)
    assert any(c["id"] == cid and c["is_archived"] is True for c in archived)


async def test_search_by_title(client):
    h = auth_headers()
    await client.post("/api/conversations", json={"title": "quarterly report"}, headers=h)
    await client.post("/api/conversations", json={"title": "random notes"}, headers=h)
    res = (await client.get("/api/conversations?q=quarterly", headers=h)).json()
    assert len(res) == 1
    assert res[0]["title"] == "quarterly report"


async def test_pin_cross_user_isolation(client):
    h1 = auth_headers()
    cid = (await client.post("/api/conversations", json={"title": "mine"}, headers=h1)).json()["id"]
    reg = await client.post(
        "/api/auth/register",
        json={"email": "pin-other@example.com", "username": "pin-other", "password": "Passw0rd!"},
    )
    h2 = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    # Another user cannot pin/edit a conversation they don't own -> 404.
    r = await client.patch(f"/api/conversations/{cid}", json={"pinned": True}, headers=h2)
    assert r.status_code == 404


async def _seed_thread(db_session) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a conv with user/assistant/user; return (conv_id, last_user_msg_id)."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    conv = Conversation(user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"), title="thread")
    db_session.add(conv)
    await db_session.flush()
    m1 = Message(conversation_id=conv.id, role="user", content="first", created_at=base)
    m2 = Message(conversation_id=conv.id, role="assistant", content="reply1", created_at=base + timedelta(minutes=1))
    m3 = Message(conversation_id=conv.id, role="user", content="second", created_at=base + timedelta(minutes=2))
    db_session.add_all([m1, m2, m3])
    await db_session.commit()
    await db_session.refresh(m3)
    return conv.id, m3.id


async def test_branch_copies_prior_history(client, db_session):
    h = auth_headers()
    conv_id, msg_id = await _seed_thread(db_session)
    r = await client.post(
        f"/api/conversations/{conv_id}/branch",
        json={"message_id": str(msg_id), "new_content": "edited second"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] != str(conv_id)
    assert body["parent_conversation_id"] == str(conv_id)
    assert body["branch_from_message_id"] == str(msg_id)
    # The two messages before the edited one were copied (user + assistant).
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant"]
    # Source conversation is untouched.
    src = (await client.get(f"/api/conversations/{conv_id}", headers=h)).json()
    assert len(src["messages"]) == 3
