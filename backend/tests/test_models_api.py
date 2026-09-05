"""Model-config API: CRUD + connectivity test, and the API-key-never-echoed rule."""
from __future__ import annotations

import json

import pytest

from tests.conftest import auth_headers


async def test_create_list_model_and_key_is_masked(client):
    h = auth_headers()
    created = await client.post(
        "/api/models",
        json={
            "name": "Mock",
            "provider": "openai-compatible",
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


async def test_model_capabilities_round_trip_through_api(client):
    h = auth_headers()
    created = await client.post(
        "/api/models",
        json={
            "name": "Reasoner",
            "provider": "openai-compatible",
            "api_base_url": "http://localhost/v1",
            "model_name": "reasoner",
            "supports_tools": True,
            "supports_parallel_tools": True,
            "supports_audio_input": True,
            "supports_audio_output": True,
            "supports_image_generation": True,
            "supports_structured_output": True,
            "supports_reasoning_effort": True,
            "output_token_parameter": "max_completion_tokens",
        },
        headers=h,
    )

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["supports_parallel_tools"] is True
    assert body["supports_audio_input"] is True
    assert body["supports_audio_output"] is True
    assert body["supports_image_generation"] is True
    assert body["supports_structured_output"] is True
    assert body["supports_reasoning_effort"] is True
    assert body["output_token_parameter"] == "max_completion_tokens"

    updated = await client.put(
        f"/api/models/{body['id']}",
        json={"supports_parallel_tools": False, "output_token_parameter": "max_tokens"},
        headers=h,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["supports_parallel_tools"] is False
    assert updated.json()["output_token_parameter"] == "max_tokens"


async def test_model_update_rejects_null_non_nullable_capability(client):
    h = auth_headers()
    created = await client.post(
        "/api/models",
        json={
            "name": "Null guard",
            "provider": "openai-compatible",
            "api_base_url": "http://localhost/v1",
            "model_name": "mock-model",
        },
        headers=h,
    )
    assert created.status_code == 201, created.text

    updated = await client.put(
        f"/api/models/{created.json()['id']}",
        json={"max_context_tokens": None},
        headers=h,
    )

    assert updated.status_code == 422


async def test_model_validation_error_never_echoes_api_key_or_raw_input(client):
    secret = "sk-plaintext-must-never-echo"
    response = await client.post(
        "/api/models",
        json={
            "name": "Secret validation",
            "provider": "openai-compatible",
            "api_base_url": "http://localhost/v1",
            "api_key": secret,
            "model_name": "mock-model",
            "supports_parallel_tools": True,
            "supports_tools": False,
        },
        headers=auth_headers(),
    )

    assert response.status_code == 422
    assert secret not in response.text
    errors = response.json()["details"]["errors"]
    assert all("input" not in error for error in errors)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", float("nan")),
        ("temperature", float("inf")),
        ("temperature", float("-inf")),
        ("temperature", -0.01),
        ("temperature", 2.01),
        ("top_p", float("nan")),
        ("top_p", float("inf")),
        ("top_p", float("-inf")),
        ("top_p", 0.0),
        ("top_p", 1.01),
    ],
)
@pytest.mark.parametrize("operation", ["create", "update"])
async def test_model_api_rejects_invalid_sampling_without_echoing_input_or_secret(
    client, operation: str, field: str, value: float
):
    secret = "sk-nonfinite-must-never-echo"
    headers = auth_headers()
    if operation == "create":
        method = "POST"
        path = "/api/models"
        payload = {
            "name": "Invalid sampling",
            "provider": "openai-compatible",
            "api_base_url": "http://localhost/v1",
            "api_key": secret,
            "model_name": "mock-model",
            field: value,
        }
    else:
        created = await client.post(
            "/api/models",
            json={
                "name": "Invalid sampling update",
                "provider": "openai-compatible",
                "api_base_url": "http://localhost/v1",
                "model_name": "mock-model",
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        method = "PUT"
        path = f"/api/models/{created.json()['id']}"
        payload = {"api_key": secret, field: value}

    response = await client.request(
        method,
        path,
        content=json.dumps(payload, allow_nan=True),
        headers={**headers, "Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert secret not in response.text
    errors = response.json()["details"]["errors"]
    assert all("input" not in error and "ctx" not in error for error in errors)


async def test_disabling_tools_also_disables_parallel_tools(client):
    h = auth_headers()
    created = await client.post(
        "/api/models",
        json={
            "name": "Parallel model",
            "provider": "openai-compatible",
            "api_base_url": "http://localhost/v1",
            "model_name": "mock-model",
            "supports_tools": True,
            "supports_parallel_tools": True,
        },
        headers=h,
    )
    assert created.status_code == 201, created.text

    updated = await client.put(
        f"/api/models/{created.json()['id']}",
        json={"supports_tools": False},
        headers=h,
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["supports_tools"] is False
    assert updated.json()["supports_parallel_tools"] is False


async def test_model_test_endpoint_ok(client, monkeypatch):
    h = auth_headers()
    created = await client.post(
        "/api/models",
        json={
            "name": "Mock2",
            "provider": "openai-compatible",
            "api_base_url": "http://localhost/v1",
            "model_name": "mock-model",
        },
        headers=h,
    )
    mid = created.json()["id"]

    # Offline stub: the /test endpoint's job under test is the ok/latency/sample
    # envelope around a provider call, not the network itself.
    from app.providers import registry as provider_registry
    from app.providers.base import ChatResult

    class OkStub:
        async def chat(self, messages, options=None):
            return ChatResult(content="pong", finish_reason="stop", usage={})

    monkeypatch.setattr(
        provider_registry, "get_provider_for_config", lambda cfg: OkStub()
    )

    result = await client.post(f"/api/models/{mid}/test", headers=h)
    assert result.status_code == 200
    assert result.json()["ok"] is True
    assert result.json()["sample"] == "pong"


async def test_delete_model(client):
    h = auth_headers()
    created = await client.post(
        "/api/models",
        json={
            "name": "MockDel",
            "provider": "openai-compatible",
            "api_base_url": "http://localhost/v1",
            "model_name": "mock-model",
        },
        headers=h,
    )
    mid = created.json()["id"]
    deleted = await client.delete(f"/api/models/{mid}", headers=h)
    assert deleted.status_code == 204
