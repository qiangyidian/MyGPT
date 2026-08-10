"""Provider-layer unit tests (no DB, no network).

MockProvider is the offline stand-in, so these also pin the behaviour the chat
stream relies on: chat returns content, stream_chat yields content deltas, and
embeddings return vectors of the configured dimension.
"""
from __future__ import annotations

import httpx
import pytest

from app.agents.policies.budget_policy import BudgetGuard, BudgetLimits
from app.agents.schemas import BudgetExceeded
from app.agents.token_budget import PromptAdmissionError
from app.core.config import get_settings
from app.model_capabilities import ModelCapabilities
from app.providers.base import ChatOptions, ProviderError
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


async def test_usage_only_empty_choices_is_emitted_even_without_done_marker():
    p = OpenAICompatibleProvider(base_url="http://x/v1", model="m")
    deltas = await _collect(p, _sse(
        '{"choices":[{"delta":{},"finish_reason":"length"}]}',
        '{"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}',
    ))

    usage_deltas = [delta for delta in deltas if delta.usage]
    assert len(usage_deltas) == 1
    assert usage_deltas[0].usage == {
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
    }
    assert usage_deltas[0].finish_reason is None
    assert [d.finish_reason for d in deltas if d.finish_reason] == ["length"]


async def test_usage_attached_to_terminal_choice_is_retained_once():
    p = OpenAICompatibleProvider(base_url="http://x/v1", model="m")
    deltas = await _collect(p, _sse(
        '{"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}',
        "[DONE]",
    ))

    usage_deltas = [delta for delta in deltas if delta.usage]
    assert [delta.usage for delta in usage_deltas] == [
        {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
    ]
    assert usage_deltas[0].finish_reason is None


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


class _RecordingOpenAIProvider(OpenAICompatibleProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.requests = []

    async def _request(self, client, url, payload):
        self.requests.append(payload)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
            request=httpx.Request("POST", url),
        )


async def test_openai_provider_rejects_oversized_messages_before_http_dispatch():
    provider = _RecordingOpenAIProvider(
        base_url="http://x/v1",
        model="m",
        capabilities=ModelCapabilities(context_window=1_000, max_output_tokens=200),
    )

    with pytest.raises(PromptAdmissionError) as exc_info:
        await provider.chat(
            [{"role": "user", "content": "oversized-item " * 10_000}],
            ChatOptions(max_tokens=200),
        )

    assert exc_info.value.code == "prompt_too_large"
    assert provider.requests == []


async def test_openai_provider_rejects_oversized_tool_schema_before_stream_http(
    monkeypatch,
):
    provider = OpenAICompatibleProvider(
        base_url="http://x/v1",
        model="m",
        capabilities=ModelCapabilities(context_window=1_000, max_output_tokens=200),
    )
    constructed = 0

    def forbidden_client(*args, **kwargs):
        nonlocal constructed
        constructed += 1
        raise AssertionError("HTTP client must not be constructed")

    monkeypatch.setattr("app.providers.openai_compatible.httpx.AsyncClient", forbidden_client)
    options = ChatOptions(
        max_tokens=200,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "huge_tool",
                    "description": "schema-item " * 10_000,
                },
            }
        ],
    )

    with pytest.raises(PromptAdmissionError):
        async for _delta in provider.stream_chat(
            [{"role": "user", "content": "use a tool"}], options
        ):
            pass

    assert constructed == 0


async def test_openai_provider_clamps_requested_output_to_model_capability():
    provider = _RecordingOpenAIProvider(
        base_url="http://x/v1",
        model="m",
        output_token_parameter="max_completion_tokens",
        capabilities=ModelCapabilities(context_window=4_000, max_output_tokens=200),
    )

    await provider.chat(
        [{"role": "user", "content": "bounded"}],
        ChatOptions(max_tokens=999, output_token_parameter="max_completion_tokens"),
    )

    assert len(provider.requests) == 1
    assert provider.requests[0]["max_completion_tokens"] == 200
    assert "max_tokens" not in provider.requests[0]


async def test_openai_provider_uses_configured_output_parameter_over_option_default():
    provider = _RecordingOpenAIProvider(
        base_url="http://x/v1",
        model="m",
        output_token_parameter="max_completion_tokens",
        capabilities=ModelCapabilities(context_window=4_000, max_output_tokens=200),
    )

    await provider.chat(
        [{"role": "user", "content": "bounded"}],
        ChatOptions(max_tokens=100),
    )

    assert provider.requests[0]["max_completion_tokens"] == 100
    assert "max_tokens" not in provider.requests[0]


@pytest.mark.parametrize(
    "options",
    [
        ChatOptions(max_tokens=200, stop=["stop-item " * 10_000]),
        ChatOptions(
            max_tokens=200,
            tools=[{"type": "function", "function": {"name": "small_tool"}}],
            tool_choice={"payload": "choice-item " * 10_000},
        ),
    ],
    ids=["stop", "tool_choice"],
)
async def test_openai_provider_budgets_supplemental_payload_before_dispatch(options):
    provider = _RecordingOpenAIProvider(
        base_url="http://x/v1",
        model="m",
        capabilities=ModelCapabilities(context_window=1_000, max_output_tokens=200),
    )

    with pytest.raises(PromptAdmissionError):
        await provider.chat([{"role": "user", "content": "small"}], options)

    assert provider.requests == []


async def test_openai_provider_rejects_extra_override_before_chat_dispatch():
    provider = _RecordingOpenAIProvider(
        base_url="http://x/v1",
        model="m",
        capabilities=ModelCapabilities(context_window=4_000, max_output_tokens=200),
    )

    with pytest.raises(PromptAdmissionError):
        await provider.chat(
            [{"role": "user", "content": "small"}],
            ChatOptions(
                extra={
                    "messages": [{"role": "user", "content": "x" * 50_000}]
                }
            ),
        )

    assert provider.requests == []


async def test_openai_provider_rejects_extra_override_before_stream_client(
    monkeypatch,
):
    provider = OpenAICompatibleProvider(base_url="http://x/v1", model="m")
    constructed = 0

    def forbidden_client(*args, **kwargs):
        nonlocal constructed
        constructed += 1
        raise AssertionError("HTTP client must not be constructed")

    monkeypatch.setattr(
        "app.providers.openai_compatible.httpx.AsyncClient", forbidden_client
    )

    with pytest.raises(PromptAdmissionError):
        async for _delta in provider.stream_chat(
            [{"role": "user", "content": "small"}],
            ChatOptions(extra={"tools": [{"description": "x" * 50_000}]}),
        ):
            pass

    assert constructed == 0


async def test_openai_provider_http_error_does_not_expose_upstream_body():
    secret = "upstream-secret-must-not-escape"

    class ErrorProvider(_RecordingOpenAIProvider):
        async def _request(self, client, url, payload):
            return httpx.Response(
                400,
                text=f"bad request echoed {secret}",
                request=httpx.Request("POST", url),
            )

    provider = ErrorProvider(base_url="http://x/v1", model="m")

    with pytest.raises(ProviderError) as exc_info:
        await provider.chat([{"role": "user", "content": "small"}])

    assert secret not in str(exc_info.value)


async def test_openai_provider_stream_error_does_not_expose_upstream_body(
    monkeypatch,
):
    secret = "stream-secret-must-not-escape"

    class ErrorResponse:
        status_code = 400

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aread(self):
            return f"bad request echoed {secret}".encode()

    class ErrorClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            return ErrorResponse()

    monkeypatch.setattr(
        "app.providers.openai_compatible.httpx.AsyncClient", ErrorClient
    )
    provider = OpenAICompatibleProvider(base_url="http://x/v1", model="m")

    with pytest.raises(ProviderError) as exc_info:
        async for _delta in provider.stream_chat(
            [{"role": "user", "content": "small"}]
        ):
            pass

    assert secret not in str(exc_info.value)


async def test_openai_stream_retry_gate_stops_before_second_http_attempt(monkeypatch):
    calls = 0

    class RetryableResponse:
        status_code = 503

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aread(self):
            return b"unavailable"

    class RetryClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            return RetryableResponse()

    monkeypatch.setattr(
        "app.providers.openai_compatible.httpx.AsyncClient", RetryClient
    )
    guard = BudgetGuard(BudgetLimits(max_agent_steps=1))
    guard.enter_step()  # the runtime owns the initial logical dispatch

    def retry_gate(attempt: int) -> float:
        guard.enter_step()
        return guard.remaining_seconds

    provider = OpenAICompatibleProvider(base_url="http://x/v1", model="m")
    with pytest.raises(BudgetExceeded, match="max agent steps"):
        async for _delta in provider.stream_chat(
            [{"role": "user", "content": "small"}],
            ChatOptions(retry_gate=retry_gate),
        ):
            pass

    assert calls == 1


async def test_openai_provider_uses_fixed_vision_reserve_not_data_url_length():
    provider = _RecordingOpenAIProvider(
        base_url="http://x/v1",
        model="m",
        capabilities=ModelCapabilities(context_window=2_000, max_output_tokens=200),
    )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this image"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{'A' * 50_000}"},
                },
            ],
        }
    ]

    await provider.chat(messages, ChatOptions(max_tokens=200))

    assert len(provider.requests) == 1
