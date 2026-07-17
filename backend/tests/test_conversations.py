"""Conversation CRUD + cross-user ownership isolation."""
from __future__ import annotations

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
