"""Model-config API: CRUD + connectivity test, and the API-key-never-echoed rule."""
from __future__ import annotations

from tests.conftest import auth_headers


async def test_create_list_model_and_key_is_masked(client):
    h = auth_headers()
    created = await client.post(
        "/api/models",
        json={
            "name": "Mock",
            "provider": "mock",
            "api_base_url": "http://localhost/v1",
            "api_key": "super-secret-key-12345",
            "model_name": "mock-model",
            "supports_stream": True,
            "supports_tools": False,
            "is_embedding": False,
        },
        headers=h,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    # The raw key must never appear in the response; only a masked version is sent.
    assert "super-secret-key-12345" not in created.text
    assert body["has_key"] is True
    assert body["api_key_masked"]
    mid = body["id"]

    listed = await client.get("/api/models", headers=h)
    assert listed.status_code == 200
    assert any(m["id"] == mid for m in listed.json())


async def test_model_test_endpoint_ok(client):
    h = auth_headers()
    created = await client.post(
        "/api/models",
        json={
            "name": "Mock2",
            "provider": "mock",
            "api_base_url": "http://localhost/v1",
            "model_name": "mock-model",
        },
        headers=h,
    )
    mid = created.json()["id"]
    result = await client.post(f"/api/models/{mid}/test", headers=h)
    assert result.status_code == 200
    assert result.json()["ok"] is True


async def test_delete_model(client):
    h = auth_headers()
    created = await client.post(
        "/api/models",
        json={
            "name": "MockDel",
            "provider": "mock",
            "api_base_url": "http://localhost/v1",
            "model_name": "mock-model",
        },
        headers=h,
    )
    mid = created.json()["id"]
    deleted = await client.delete(f"/api/models/{mid}", headers=h)
    assert deleted.status_code == 204
