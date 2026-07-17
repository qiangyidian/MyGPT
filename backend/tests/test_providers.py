"""Provider-layer unit tests (no DB, no network).

MockProvider is the offline stand-in, so these also pin the behaviour the chat
stream relies on: chat returns content, stream_chat yields content deltas, and
embeddings return vectors of the configured dimension.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.providers.mock import MockProvider


async def test_mock_chat_returns_content():
    provider = MockProvider(base_url="http://localhost/v1", model="mock-model")
    result = await provider.chat([{"role": "user", "content": "hi"}])
    assert result.content
    assert result.finish_reason == "stop"


async def test_mock_stream_yields_tokens():
    provider = MockProvider(base_url="http://localhost/v1", model="mock-model")
    deltas = []
    async for chunk in provider.stream_chat([{"role": "user", "content": "hello there"}]):
        if chunk.content:
            deltas.append(chunk.content)
    assert "".join(deltas), "stream produced no content"


async def test_mock_embeddings_dim_matches_config():
    provider = MockProvider(base_url="http://localhost/v1", model="mock-model")
    vectors = await provider.embeddings(["alpha", "beta"])
    assert len(vectors) == 2
    dim = get_settings().QDRANT_EMBEDDING_DIM
    assert all(len(v) == dim for v in vectors), f"expected dim {dim}"


async def test_mock_embeddings_are_deterministic():
    provider = MockProvider(base_url="http://localhost/v1", model="mock-model")
    a = await provider.embeddings(["same text"])
    b = await provider.embeddings(["same text"])
    assert a[0] == b[0]
