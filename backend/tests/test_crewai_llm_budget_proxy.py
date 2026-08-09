from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.adapters import llm_adapter
from app.agents.runtime.crewai_runtime import CrewAIRuntime
from app.agents.token_budget import PromptAdmissionError


class FakeCrewAILLM:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.sync_calls = []
        self.async_calls = []
        self.compatibility_marker = object()

    def call(self, *args, **kwargs):
        self.sync_calls.append((args, kwargs))
        return "sync-result"

    async def acall(self, *args, **kwargs):
        self.async_calls.append((args, kwargs))
        return "async-result"


def _config(*, context_window: int = 4_000, max_tokens: int = 200):
    return SimpleNamespace(
        max_context_tokens=context_window,
        max_tokens=max_tokens,
        model_name="gpt-4o",
        output_token_parameter="max_tokens",
    )


def _wrap(llm, cfg):
    assert hasattr(llm_adapter, "wrap_crewai_llm_with_budget")
    return llm_adapter.wrap_crewai_llm_with_budget(llm, cfg)


def test_budget_proxy_accepts_sync_payload_and_delegates_unchanged_once():
    underlying = FakeCrewAILLM(model="gpt-4o")
    messages = [{"role": "user", "content": "bounded request"}]
    tools = [{"type": "function", "function": {"name": "search"}}]

    wrapped = _wrap(underlying, _config())
    result = wrapped.call(messages, tools=tools, callbacks=["callback"])

    assert result == "sync-result"
    assert wrapped is underlying
    assert wrapped.compatibility_marker is underlying.compatibility_marker
    assert len(underlying.sync_calls) == 1
    args, kwargs = underlying.sync_calls[0]
    assert args[0] is messages
    assert kwargs["tools"] is tools
    assert kwargs["callbacks"] == ["callback"]


async def test_budget_proxy_accepts_async_payload_and_delegates_unchanged_once():
    underlying = FakeCrewAILLM(model="gpt-4o")
    messages = [{"role": "user", "content": "bounded async request"}]
    tools = [{"type": "function", "function": {"name": "search"}}]

    wrapped = _wrap(underlying, _config())
    result = await wrapped.acall(messages=messages, tools=tools)

    assert result == "async-result"
    assert len(underlying.async_calls) == 1
    args, kwargs = underlying.async_calls[0]
    assert args == ()
    assert kwargs["messages"] is messages
    assert kwargs["tools"] is tools


def test_budget_proxy_rejects_oversized_initial_payload_without_delegating():
    underlying = FakeCrewAILLM(model="gpt-4o")
    messages = [{"role": "user", "content": "oversized-item " * 10_000}]
    wrapped = _wrap(underlying, _config(context_window=1_000))

    with pytest.raises(PromptAdmissionError) as exc_info:
        wrapped.call(messages)

    assert exc_info.value.code == "prompt_too_large"
    assert underlying.sync_calls == []


def test_budget_proxy_reserves_serialized_tool_schemas_before_delegating():
    underlying = FakeCrewAILLM(model="gpt-4o")
    messages = [{"role": "user", "content": "use a tool"}]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "huge_tool",
                "description": "schema-item " * 10_000,
                "parameters": {"type": "object"},
            },
        }
    ]
    wrapped = _wrap(underlying, _config(context_window=1_000))

    with pytest.raises(PromptAdmissionError):
        wrapped.call(messages, tools=tools)

    assert underlying.sync_calls == []


async def test_budget_proxy_rejects_oversized_followup_tool_result_without_delegating():
    underlying = FakeCrewAILLM(model="gpt-4o")
    messages = [
        {"role": "system", "content": "agent scaffolding"},
        {"role": "user", "content": "search"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "search", "arguments": "{}"}}
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "tool-result-item " * 10_000,
        },
    ]
    wrapped = _wrap(underlying, _config(context_window=1_000))

    with pytest.raises(PromptAdmissionError):
        await wrapped.acall(messages, tools=[{"name": "search"}])

    assert underlying.async_calls == []


@pytest.mark.parametrize(
    ("enable_tools", "profile", "expected_path"),
    [
        (False, "general", "single"),
        (True, "deep_research", "multi"),
    ],
)
async def test_runtime_routes_factory_budget_decorator_to_both_crewai_paths(
    monkeypatch, enable_tools: bool, profile: str, expected_path: str
):
    monkeypatch.setattr("crewai.LLM", FakeCrewAILLM)
    captured = {}

    async def fake_single(self, ctx, llm, intent):
        captured["path"] = "single"
        captured["llm"] = llm
        if False:
            yield None

    async def fake_multi(self, ctx, llm, selected_profile):
        captured["path"] = "multi"
        captured["llm"] = llm
        if False:
            yield None

    monkeypatch.setattr(CrewAIRuntime, "_run_single_agent", fake_single)
    monkeypatch.setattr(CrewAIRuntime, "_run_multi_agent", fake_multi)
    cfg = SimpleNamespace(
        api_key_encrypted="",
        model_name="gpt-4o",
        api_base_url="http://localhost/v1",
        temperature=0.3,
        top_p=1.0,
        max_tokens=200,
        max_context_tokens=4_000,
        output_token_parameter="max_tokens",
    )
    ctx = SimpleNamespace(
        model_config=cfg,
        enable_tools=enable_tools,
        user_content="research this",
        extra={},
        agent_profile=profile,
    )

    events = [event async for event in CrewAIRuntime().stream_turn(ctx)]

    assert events == []
    assert captured["path"] == expected_path
    assert getattr(captured["llm"], "_model_budget_guarded", False) is True


async def test_lightweight_crewai_admission_error_is_a_controlled_sse_terminal(
    monkeypatch
):
    from crewai import Crew

    async def reject_kickoff(self):
        raise PromptAdmissionError("prompt_too_large", "final payload exceeds budget")

    monkeypatch.setattr(Crew, "kickoff_async", reject_kickoff)
    cfg = SimpleNamespace(
        api_key_encrypted="",
        model_name="gpt-4o",
        api_base_url="http://localhost/v1",
        temperature=0.3,
        top_p=1.0,
        max_tokens=200,
        max_context_tokens=4_000,
        output_token_parameter="max_tokens",
    )
    llm = llm_adapter.CrewAILLMFactory.from_model_config(cfg)
    ctx = SimpleNamespace(
        enable_tools=False,
        user_content="bounded request",
        assistant_msg=SimpleNamespace(content=""),
        extra={},
    )

    events = [
        event
        async for event in CrewAIRuntime()._run_single_agent(ctx, llm, "chat")
    ]

    errors = [event.data for event in events if event.kind == "error"]
    assert errors[-1] == {
        "code": "prompt_too_large",
        "message": "final payload exceeds budget",
    }
    assert ctx.extra["finish_reason"] == "budget"
    assert not any(event.kind == "done" for event in events)
