"""Provider-layer unit tests (no DB, no network).

MockProvider is the offline stand-in, so these also pin the behaviour the chat
stream relies on: chat returns content, stream_chat yields content deltas, and
embeddings return vectors of the configured dimension.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.providers.mock import MockProvider
from app.providers.openai_compatible import OpenAICompatibleProvider


class _FakeSSEResponse:
    """Stand-in for an httpx streaming response: yields pre-split SSE lines."""

    def __init__(self, lines):
        self._lines = list(lines)

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


def _sse(*payloads: str) -> list[str]:
    """Build SSE lines: each `data: <payload>` followed by a blank separator."""
    lines: list[str] = []
    for p in payloads:
        lines.append(f"data: {p}")
        lines.append("")
    return lines


async def _collect(provider, lines):
    out = []
    async for d in provider._iter_sse(_FakeSSEResponse(lines)):
        out.append(d)
    return out


async def test_done_does_not_override_length_finish_reason():
    # The headline bug: token -> finish=length -> [DONE] must end as `length`,
    # NOT be clobbered by a synthetic `stop` from the [DONE] marker.
    p = OpenAICompatibleProvider(base_url="http://x/v1", model="m")
    deltas = await _collect(p, _sse(
        '{"choices":[{"delta":{"content":"abc"}}]}',
        '{"choices":[{"delta":{},"finish_reason":"length"}]}',
        "[DONE]",
    ))
    finishes = [d.finish_reason for d in deltas if d.finish_reason]
    assert finishes == ["length"], f"[DONE] must not append a fake stop: {finishes}"


async def test_done_falls_back_to_stop_with_no_explicit_finish():
    p = OpenAICompatibleProvider(base_url="http://x/v1", model="m")
    deltas = await _collect(p, _sse(
        '{"choices":[{"delta":{"content":"abc"}}]}',
        "[DONE]",
    ))
    finishes = [d.finish_reason for d in deltas if d.finish_reason]
    assert finishes == ["stop"], finishes


async def test_content_and_finish_in_same_chunk_preserved():
    p = OpenAICompatibleProvider(base_url="http://x/v1", model="m")
    deltas = await _collect(p, _sse(
        '{"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}',
        "[DONE]",
    ))
    assert any(d.content == "done" for d in deltas)
    finishes = [d.finish_reason for d in deltas if d.finish_reason]
    assert finishes == ["stop"], finishes


async def test_usage_only_chunk_does_not_end_generation():
    p = OpenAICompatibleProvider(base_url="http://x/v1", model="m")
    deltas = await _collect(p, _sse(
        '{"choices":[{"delta":{"content":"x"}}]}',
        '{"choices":[],"usage":{"prompt_tokens":1}}',
        '{"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "[DONE]",
    ))
    finishes = [d.finish_reason for d in deltas if d.finish_reason]
    assert finishes == ["stop"], finishes


async def test_tool_calls_finish_not_overridden_by_done():
    p = OpenAICompatibleProvider(base_url="http://x/v1", model="m")
    deltas = await _collect(p, _sse(
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"f","arguments":"{}"}}]}}]}',
        '{"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "[DONE]",
    ))
    finishes = [d.finish_reason for d in deltas if d.finish_reason]
    assert finishes, "expected a tool_calls finish"
    assert all(f == "tool_calls" for f in finishes), finishes
    assert "stop" not in finishes, "[DONE] must not override tool_calls with stop"
    assert any(d.tool_calls for d in deltas), "tool calls must be flushed"


async def test_bare_done_after_tool_calls_does_not_clobber():
    # Non-conformant provider (vLLM/Ollama shim): tool_call deltas then a BARE
    # [DONE] with no terminal finish chunk. Must end as tool_calls, not a
    # synthetic stop that would drop the tool calls + false-complete.
    p = OpenAICompatibleProvider(base_url="http://x/v1", model="m")
    deltas = await _collect(p, _sse(
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"f","arguments":"{}"}}]}}]}',
        "[DONE]",
    ))
    finishes = [d.finish_reason for d in deltas if d.finish_reason]
    assert finishes == ["tool_calls"], f"bare [DONE] must not clobber tool_calls: {finishes}"
    assert any(d.tool_calls for d in deltas), "tool calls must be flushed"


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
