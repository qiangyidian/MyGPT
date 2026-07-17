"""Auth flow: register -> login -> /me, plus duplicate + bad-password guards."""
from __future__ import annotations


async def test_register_login_me_round_trip(client):
    reg = await client.post(
        "/api/auth/register",
        json={"email": "alice@example.com", "username": "alice", "password": "Passw0rd!"},
    )
    assert reg.status_code == 201, reg.text
    token = reg.json()["access_token"]

    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "alice@example.com"


async def test_login_with_correct_password(client):
    await client.post(
        "/api/auth/register",
        json={"email": "bob@example.com", "username": "bob", "password": "Passw0rd!"},
    )
    r = await client.post(
        "/api/auth/login",
        json={"email": "bob@example.com", "password": "Passw0rd!"},
    )
    assert r.status_code == 200
    assert "access_token" in r.json()


async def test_login_bad_password(client):
    await client.post(
        "/api/auth/register",
        json={"email": "carol@example.com", "username": "carol", "password": "Passw0rd!"},
    )
    r = await client.post(
        "/api/auth/login",
        json={"email": "carol@example.com", "password": "wrong-password"},
    )
    assert r.status_code == 401


async def test_duplicate_register_conflicts(client):
    payload = {"email": "dave@example.com", "username": "dave", "password": "Passw0rd!"}
    first = await client.post("/api/auth/register", json=payload)
    assert first.status_code == 201
    second = await client.post("/api/auth/register", json=payload)
    assert second.status_code == 409
