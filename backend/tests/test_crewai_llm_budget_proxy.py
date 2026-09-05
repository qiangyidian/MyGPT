from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from app.agents.adapters import llm_adapter
from app.agents.policies.budget_policy import BudgetGuard, BudgetLimits
from app.agents.runtime.crewai_runtime import CrewAIRuntime
from app.agents.schemas import BudgetExceeded
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


class MeteredCrewAILLM(FakeCrewAILLM):
    def __init__(
        self, *, per_call=5, fail=False, fail_sync=False, fail_async=False, **kwargs
    ):
        super().__init__(**kwargs)
        self.total = 0
        self.cost = 0.0
        self.per_call = per_call
        self.fail = fail
        self.fail_sync = fail_sync
        self.fail_async = fail_async
        self.active = 0
        self.max_active = 0

    def get_token_usage_summary(self):
        return {"total_tokens": self.total, "cost_usd": self.cost}

    def call(self, *args, **kwargs):
        self.sync_calls.append((args, kwargs))
        # Keep the cumulative before/call/after window open long enough for a
        # concurrent async entry point to overlap in the regression tests.
        time.sleep(0.02)
        self.total += self.per_call
        self.cost += 0.2
        if self.fail or self.fail_sync:
            raise RuntimeError("metered provider failure")
        return "sync-result"

    async def acall(self, *args, **kwargs):
        self.async_calls.append((args, kwargs))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            self.total += self.per_call
            self.cost += 0.2
            if self.fail or self.fail_async:
                raise RuntimeError("metered provider failure")
            return "async-result"
        finally:
            self.active -= 1


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


async def test_budget_proxy_gates_every_internal_async_model_turn():
    underlying = FakeCrewAILLM(model="gpt-4o")
    guard = BudgetGuard(BudgetLimits(max_agent_steps=1))
    wrapped = llm_adapter.wrap_crewai_llm_with_budget(
        underlying, _config(), budget_guard=guard
    )

    assert await wrapped.acall(messages=[{"role": "user", "content": "one"}]) == "async-result"
    with pytest.raises(BudgetExceeded, match="max agent steps"):
        await wrapped.acall(messages=[{"role": "user", "content": "two"}])

    assert len(underlying.async_calls) == 1


async def test_budget_proxy_charges_each_call_before_next_dispatch():
    underlying = MeteredCrewAILLM(model="gpt-4o", per_call=10)
    guard = BudgetGuard(BudgetLimits(max_total_tokens=10, max_cost_usd=1.0))
    wrapped = llm_adapter.wrap_crewai_llm_with_budget(
        underlying, _config(), budget_guard=guard
    )

    assert await wrapped.acall(messages=[{"role": "user", "content": "one"}]) == "async-result"
    assert guard.tokens_used == 10
    assert guard.cost_usd_used == pytest.approx(0.2)
    with pytest.raises(BudgetExceeded, match="token budget"):
        await wrapped.acall(messages=[{"role": "user", "content": "two"}])
    assert len(underlying.async_calls) == 1


def test_budget_proxy_sync_call_charges_before_next_dispatch():
    underlying = MeteredCrewAILLM(model="gpt-4o", per_call=10)
    guard = BudgetGuard(BudgetLimits(max_total_tokens=10, max_cost_usd=1.0))
    wrapped = llm_adapter.wrap_crewai_llm_with_budget(
        underlying, _config(), budget_guard=guard
    )

    assert wrapped.call([{"role": "user", "content": "one"}]) == "sync-result"
    assert guard.tokens_used == 10
    assert guard.cost_usd_used == pytest.approx(0.2)
    with pytest.raises(BudgetExceeded, match="token budget"):
        wrapped.call([{"role": "user", "content": "two"}])
    assert len(underlying.sync_calls) == 1


async def test_budget_proxy_charges_failed_call_immediately():
    underlying = MeteredCrewAILLM(model="gpt-4o", per_call=4, fail=True)
    guard = BudgetGuard(BudgetLimits(max_total_tokens=100, max_cost_usd=1.0))
    wrapped = llm_adapter.wrap_crewai_llm_with_budget(
        underlying, _config(), budget_guard=guard
    )

    with pytest.raises(RuntimeError, match="metered provider failure"):
        await wrapped.acall(messages=[{"role": "user", "content": "fail"}])
    assert guard.tokens_used == 4
    assert guard.cost_usd_used == pytest.approx(0.2)


async def test_budget_proxy_concurrent_calls_do_not_double_count():
    underlying = MeteredCrewAILLM(model="gpt-4o", per_call=4)
    guard = BudgetGuard(BudgetLimits(max_total_tokens=100, max_cost_usd=2.0))
    wrapped = llm_adapter.wrap_crewai_llm_with_budget(
        underlying, _config(), budget_guard=guard
    )

    await asyncio.gather(
        wrapped.acall(messages=[{"role": "user", "content": "a"}]),
        wrapped.acall(messages=[{"role": "user", "content": "b"}]),
    )

    assert guard.tokens_used == 8
    assert guard.cost_usd_used == pytest.approx(0.4)
    assert underlying.max_active == 1


async def test_budget_proxy_mixed_sync_async_calls_charge_exactly_once():
    underlying = MeteredCrewAILLM(model="gpt-4o", per_call=4)
    guard = BudgetGuard(BudgetLimits(max_total_tokens=100, max_cost_usd=2.0))
    wrapped = llm_adapter.wrap_crewai_llm_with_budget(
        underlying, _config(), budget_guard=guard
    )

    await asyncio.gather(
        wrapped.acall(messages=[{"role": "user", "content": "async"}]),
        asyncio.to_thread(
            wrapped.call, [{"role": "user", "content": "sync"}]
        ),
    )

    assert underlying.total == 8
    assert guard.tokens_used == 8
    assert guard.cost_usd_used == pytest.approx(0.4)


async def test_budget_proxy_mixed_failure_still_charges_each_call_once():
    underlying = MeteredCrewAILLM(
        model="gpt-4o", per_call=4, fail_sync=True
    )
    guard = BudgetGuard(BudgetLimits(max_total_tokens=100, max_cost_usd=2.0))
    wrapped = llm_adapter.wrap_crewai_llm_with_budget(
        underlying, _config(), budget_guard=guard
    )

    results = await asyncio.gather(
        wrapped.acall(messages=[{"role": "user", "content": "async"}]),
        asyncio.to_thread(
            wrapped.call, [{"role": "user", "content": "sync-fails"}]
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(result, RuntimeError) for result in results) == 1
    assert underlying.total == 8
    assert guard.tokens_used == 8
    assert guard.cost_usd_used == pytest.approx(0.4)


async def test_budget_proxy_sequential_mixed_calls_charge_exactly_once():
    underlying = MeteredCrewAILLM(model="gpt-4o", per_call=4)
    guard = BudgetGuard(BudgetLimits(max_total_tokens=100, max_cost_usd=2.0))
    wrapped = llm_adapter.wrap_crewai_llm_with_budget(
        underlying, _config(), budget_guard=guard
    )

    await wrapped.acall(messages=[{"role": "user", "content": "async"}])
    await asyncio.to_thread(
        wrapped.call, [{"role": "user", "content": "sync"}]
    )

    assert guard.tokens_used == 8
    assert guard.cost_usd_used == pytest.approx(0.4)


async def test_budget_proxy_without_summary_does_not_claim_realtime_usage():
    underlying = FakeCrewAILLM(model="gpt-4o")
    guard = BudgetGuard(BudgetLimits(max_total_tokens=100, max_cost_usd=2.0))
    wrapped = llm_adapter.wrap_crewai_llm_with_budget(
        underlying, _config(), budget_guard=guard
    )

    await wrapped.acall(messages=[{"role": "user", "content": "no summary"}])

    assert wrapped._usage_charged_realtime is False
    assert guard.tokens_used == 0
    assert guard.cost_usd_used == 0


async def test_budget_proxy_cancelled_async_lock_waiter_does_not_leak_lock():
    sync_started = threading.Event()
    release_sync = threading.Event()

    class BlockingMeteredLLM(MeteredCrewAILLM):
        def call(self, *args, **kwargs):
            self.sync_calls.append((args, kwargs))
            sync_started.set()
            assert release_sync.wait(timeout=2)
            self.total += self.per_call
            self.cost += 0.2
            return "sync-result"

    underlying = BlockingMeteredLLM(model="gpt-4o", per_call=4)
    guard = BudgetGuard(BudgetLimits(max_total_tokens=100, max_cost_usd=2.0))
    wrapped = llm_adapter.wrap_crewai_llm_with_budget(
        underlying, _config(), budget_guard=guard
    )
    sync_task = asyncio.create_task(
        asyncio.to_thread(
            wrapped.call, [{"role": "user", "content": "hold-lock"}]
        )
    )
    assert await asyncio.to_thread(sync_started.wait, 1)

    waiter = asyncio.create_task(
        wrapped.acall(messages=[{"role": "user", "content": "cancel-me"}])
    )
    await asyncio.sleep(0.02)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    release_sync.set()
    await sync_task
    assert await wrapped.acall(
        messages=[{"role": "user", "content": "after-cancel"}]
    ) == "async-result"
    assert guard.tokens_used == 8
    assert guard.cost_usd_used == pytest.approx(0.4)


async def test_budget_proxy_async_lock_wait_keeps_event_loop_responsive():
    sync_started = threading.Event()
    release_sync = threading.Event()

    class BlockingMeteredLLM(MeteredCrewAILLM):
        def call(self, *args, **kwargs):
            sync_started.set()
            assert release_sync.wait(timeout=2)
            self.total += self.per_call
            self.cost += 0.2
            return "sync-result"

    underlying = BlockingMeteredLLM(model="gpt-4o", per_call=4)
    guard = BudgetGuard(BudgetLimits(max_total_tokens=100, max_cost_usd=2.0))
    wrapped = llm_adapter.wrap_crewai_llm_with_budget(
        underlying, _config(), budget_guard=guard
    )
    sync_task = asyncio.create_task(
        asyncio.to_thread(
            wrapped.call, [{"role": "user", "content": "hold-lock"}]
        )
    )
    assert await asyncio.to_thread(sync_started.wait, 1)
    waiting_call = asyncio.create_task(
        wrapped.acall(messages=[{"role": "user", "content": "wait"}])
    )

    heartbeat = 0
    try:
        deadline = asyncio.get_running_loop().time() + 0.04
        while asyncio.get_running_loop().time() < deadline:
            heartbeat += 1
            await asyncio.sleep(0.005)

        assert heartbeat >= 2
        assert waiting_call.done() is False
    finally:
        release_sync.set()
    await asyncio.gather(sync_task, waiting_call)
    assert guard.tokens_used == 8


async def test_budget_proxy_sync_contention_on_event_loop_fails_fast():
    sync_started = threading.Event()
    release_sync = threading.Event()

    class BlockingMeteredLLM(MeteredCrewAILLM):
        def call(self, *args, **kwargs):
            sync_started.set()
            assert release_sync.wait(timeout=2)
            self.total += self.per_call
            self.cost += 0.2
            return "sync-result"

    underlying = BlockingMeteredLLM(model="gpt-4o", per_call=4)
    guard = BudgetGuard(BudgetLimits(max_total_tokens=100, max_cost_usd=2.0))
    wrapped = llm_adapter.wrap_crewai_llm_with_budget(
        underlying, _config(), budget_guard=guard
    )
    holder = asyncio.create_task(
        asyncio.to_thread(
            wrapped.call, [{"role": "user", "content": "hold-lock"}]
        )
    )
    assert await asyncio.to_thread(sync_started.wait, 1)

    async def invoke_sync_on_running_loop():
        with pytest.raises(RuntimeError, match="event-loop"):
            wrapped.call([{"role": "user", "content": "must-not-deadlock"}])

    contender = asyncio.create_task(
        asyncio.to_thread(lambda: asyncio.run(invoke_sync_on_running_loop()))
    )
    try:
        await asyncio.wait_for(asyncio.shield(contender), timeout=0.2)
    finally:
        release_sync.set()
        await asyncio.gather(holder, contender, return_exceptions=True)


async def test_budget_proxy_zero_summary_delta_does_not_claim_ownership():
    class ZeroUsageLLM(FakeCrewAILLM):
        def get_token_usage_summary(self):
            return {"total_tokens": 0, "cost_usd": 0.0}

    underlying = ZeroUsageLLM(model="gpt-4o")
    guard = BudgetGuard(BudgetLimits(max_total_tokens=100, max_cost_usd=2.0))
    wrapped = llm_adapter.wrap_crewai_llm_with_budget(
        underlying, _config(), budget_guard=guard
    )

    await wrapped.acall(messages=[{"role": "user", "content": "zero usage"}])

    assert wrapped._usage_charged_realtime is False
    assert wrapped._usage_charge_generation == 0
    assert guard.tokens_used == 0
    assert guard.cost_usd_used == 0


async def test_budget_proxy_locks_are_isolated_between_wrapped_llms():
    class ConcurrentLLM(MeteredCrewAILLM):
        active = 0
        max_active = 0

        async def acall(self, *args, **kwargs):
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
            try:
                await asyncio.sleep(0.02)
                self.total += self.per_call
                self.cost += 0.2
                return "async-result"
            finally:
                type(self).active -= 1

    first_guard = BudgetGuard(BudgetLimits(max_total_tokens=100, max_cost_usd=2.0))
    second_guard = BudgetGuard(BudgetLimits(max_total_tokens=100, max_cost_usd=2.0))
    first = llm_adapter.wrap_crewai_llm_with_budget(
        ConcurrentLLM(model="gpt-4o", per_call=4),
        _config(),
        budget_guard=first_guard,
    )
    second = llm_adapter.wrap_crewai_llm_with_budget(
        ConcurrentLLM(model="gpt-4o", per_call=4),
        _config(),
        budget_guard=second_guard,
    )

    await asyncio.gather(
        first.acall(messages=[{"role": "user", "content": "first"}]),
        second.acall(messages=[{"role": "user", "content": "second"}]),
    )

    assert ConcurrentLLM.max_active == 2
    assert first_guard.tokens_used == 4
    assert second_guard.tokens_used == 4


def test_budget_proxy_rejects_rebinding_to_another_run_guard():
    underlying = FakeCrewAILLM(model="gpt-4o")
    first = BudgetGuard(BudgetLimits())
    second = BudgetGuard(BudgetLimits())
    llm_adapter.wrap_crewai_llm_with_budget(
        underlying, _config(), budget_guard=first
    )

    with pytest.raises(ValueError, match="another run"):
        llm_adapter.wrap_crewai_llm_with_budget(
            underlying, _config(), budget_guard=second
        )


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
