"""Chat streaming end-to-end: a Mock-backed turn produces meta -> tokens -> done."""
from __future__ import annotations

from tests.conftest import auth_headers


async def _create_mock_model(client, headers):
    r = await client.post(
        "/api/models",
        json={
            "name": "Mock for stream",
            "provider": "mock",
            "api_base_url": "http://localhost/v1",
            "model_name": "mock-model",
            "supports_stream": True,
            "supports_tools": False,
            "is_embedding": False,
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_chat_stream_emits_meta_token_done(client):
    h = auth_headers()
    model_id = await _create_mock_model(client, h)

    events: list[str] = []
    async with client.stream(
        "POST",
        "/api/chat/stream",
        json={"content": "hello world", "model_id": model_id},
        headers=h,
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())

    assert "meta" in events, f"missing meta event in {events}"
    assert "token" in events, f"missing token event in {events}"
    assert events[-1] in ("done", "error"), f"unexpected last event {events[-1]}"
