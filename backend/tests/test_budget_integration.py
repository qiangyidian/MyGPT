"""Integration contracts for run-scoped Agent execution budgets."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agents.policies.budget_policy import BudgetGuard, BudgetLimits
from app.agents.runtime.stage_executor import CrewAIStageExecutor
from app.agents.schemas import BudgetExceeded
from app.agents.stage_context import make_stage_context
from app.core.config import Settings
from app.core.pricing import usage_cost
from app.models.conversation import Conversation
from app.models.message import Message
from app.providers.base import ChatDelta, ToolCallDef
from app.tools.base import BaseTool, ToolRegistry


def _limits(**overrides) -> BudgetLimits:
    values = {
        "max_agent_steps": 8,
        "max_tool_calls": 12,
        "max_replan_count": 2,
        "max_runtime_seconds": 120.0,
        "max_tool_output_chars": 8_000,
        "max_total_tokens": 40_000,
        "max_cost_usd": 5.0,
    }
    values.update(overrides)
    return BudgetLimits(**values)


def test_budget_limits_are_built_from_validated_settings(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_STEPS", "3")
    monkeypatch.setenv("AGENT_MAX_TOOL_CALLS", "4")
    monkeypatch.setenv("AGENT_MAX_REPLAN_COUNT", "1")
    monkeypatch.setenv("AGENT_MAX_RUNTIME_SECONDS", "2.5")
    monkeypatch.setenv("AGENT_MAX_TOOL_OUTPUT_CHARS", "321")
    monkeypatch.setenv("AGENT_MAX_TOTAL_TOKENS", "999")
    monkeypatch.setenv("AGENT_MAX_COST_USD", "0.75")

    limits = BudgetLimits.from_settings(Settings(_env_file=None))

    assert limits == BudgetLimits(
        max_agent_steps=3,
        max_tool_calls=4,
        max_replan_count=1,
        max_runtime_seconds=2.5,
        max_tool_output_chars=321,
        max_total_tokens=999,
        max_cost_usd=0.75,
    )
    with pytest.raises(FrozenInstanceError):
        limits.max_agent_steps = 99  # type: ignore[misc]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AGENT_MAX_STEPS", "0"),
        ("AGENT_MAX_RUNTIME_SECONDS", "nan"),
        ("AGENT_MAX_TOOL_OUTPUT_CHARS", "-1"),
        ("AGENT_MAX_COST_USD", "inf"),
    ],
)
def test_settings_reject_invalid_budget_values(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_per_run_overrides_can_only_reduce_limits_without_authorization():
    base = _limits(max_agent_steps=4, max_cost_usd=2.0)
    reduced = base.with_overrides({"max_agent_steps": 2, "max_cost_usd": 1.0})
    assert reduced.max_agent_steps == 2
    assert reduced.max_cost_usd == 1.0

    with pytest.raises(ValueError, match="cannot increase"):
        base.with_overrides({"max_agent_steps": 5})
    assert base.with_overrides(
        {"max_agent_steps": 5}, allow_increase=True
    ).max_agent_steps == 5


def test_tool_output_budget_rejects_caps_too_small_for_safe_truncation():
    with pytest.raises(ValueError, match="at least 16"):
        _limits(max_tool_output_chars=15)


def test_usage_delta_and_cumulative_snapshots_charge_exactly_once():
    guard = BudgetGuard(_limits(max_total_tokens=100, max_cost_usd=10.0))

    guard.add_usage(total_tokens=10, cost_usd=0.25)
    guard.add_usage(
        total_tokens=20,
        cost_usd=0.50,
        cumulative=True,
        source="provider-a",
    )
    guard.add_usage(
        total_tokens=20,
        cost_usd=0.50,
        cumulative=True,
        source="provider-a",
    )
    guard.add_usage(
        total_tokens=25,
        cost_usd=0.60,
        cumulative=True,
        source="provider-a",
    )
    guard.add_usage(
        {"total_tokens": 7, "cost_usd": 0.15}, usage_id="tool-call-1"
    )
    guard.add_usage(
        {"total_tokens": 7, "cost_usd": 0.15}, usage_id="tool-call-1"
    )

    assert guard.tokens_used == 42
    assert guard.cost_usd_used == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"total_tokens": -1}, "non-negative"),
        ({"cost_usd": float("nan")}, "finite"),
        ({"total_tokens": 1.5}, "integer"),
        ({"total_tokens": 1, "cumulative": True}, "source"),
    ],
)
def test_usage_rejects_non_monotonic_or_ambiguous_values(kwargs, message):
    guard = BudgetGuard(_limits())
    with pytest.raises(ValueError, match=message):
        guard.add_usage(**kwargs)


def test_token_and_cost_boundaries_are_exhausted_after_exact_consumption():
    token_guard = BudgetGuard(_limits(max_total_tokens=10))
    token_guard.add_usage(total_tokens=10)
    with pytest.raises(BudgetExceeded, match="token budget"):
        token_guard.check()

    cost_guard = BudgetGuard(_limits(max_cost_usd=1.0))
    cost_guard.add_usage(cost_usd=1.0)
    with pytest.raises(BudgetExceeded, match="cost budget"):
        cost_guard.check()


def test_usage_cost_prefers_actual_metered_cost_over_token_estimate(monkeypatch):
    monkeypatch.setattr("app.core.pricing.compute_cost", lambda *_args: 9.0)
    assert usage_cost(
        "priced-model",
        {"prompt_tokens": 1, "completion_tokens": 1, "cost_usd": 0.125},
    ) == pytest.approx(0.125)


def test_counted_boundaries_allow_exactly_n_and_replans_are_exposed():
    step_guard = BudgetGuard(_limits(max_agent_steps=1))
    step_guard.enter_step()
    with pytest.raises(BudgetExceeded, match="max agent steps"):
        step_guard.enter_step()

    tool_guard = BudgetGuard(_limits(max_tool_calls=1))
    tool_guard.enter_tool_call()
    with pytest.raises(BudgetExceeded, match="max tool calls"):
        tool_guard.enter_tool_call()

    replan_guard = BudgetGuard(_limits(max_replan_count=1))
    replan_guard.enter_replan()
    with pytest.raises(BudgetExceeded, match="max replans"):
        replan_guard.enter_replan()


async def test_chat_error_finalization_persists_budget_snapshot(db_session):
    """The service returns on the first error SSE, so that event is authoritative."""
    from app.services.chat_service import ChatService

    conversation = Conversation(
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        title="budget terminal",
    )
    db_session.add(conversation)
    await db_session.flush()
    message = Message(
        conversation_id=conversation.id,
        role="assistant",
        content="partial",
        metadata_={},
    )
    db_session.add(message)
    await db_session.flush()
    snapshot = BudgetGuard(_limits(max_agent_steps=1)).snapshot()

    await ChatService()._finalize_error(
        db_session,
        message,
        "Agent execution budget exceeded",
        finish_reason="budget",
        code="agent_budget_exceeded",
        budget=snapshot,
    )

    assert message.metadata_["finish_reason"] == "budget"
    assert message.metadata_["status"] == "truncated"
    assert message.metadata_["budget"] == snapshot


def test_snapshot_is_complete_serializable_and_includes_remaining_reason():
    guard = BudgetGuard(_limits(max_total_tokens=10, max_cost_usd=2.0))
    guard.enter_step()
    guard.add_usage(total_tokens=10, cost_usd=0.5)

    snapshot = guard.snapshot()

    assert snapshot["limits"] == {
        "max_agent_steps": 8,
        "max_tool_calls": 12,
        "max_replan_count": 2,
        "max_runtime_seconds": 120.0,
        "max_tool_output_chars": 8_000,
        "max_total_tokens": 10,
        "max_cost_usd": 2.0,
    }
    assert snapshot["used"]["steps"] == 1
    assert snapshot["used"]["total_tokens"] == 10
    assert snapshot["used"]["cost_usd"] == pytest.approx(0.5)
    assert snapshot["remaining"]["total_tokens"] == 0
    assert snapshot["remaining"]["cost_usd"] == pytest.approx(1.5)
    assert snapshot["exhausted"] is True
    assert "token budget" in snapshot["reason"]


async def test_crewai_stage_uses_shared_guard_remaining_timeout():
    guard = BudgetGuard(_limits(max_runtime_seconds=0.01))
    stage_ctx = make_stage_context("budget-stage", budget_guard=guard)

    class SlowAgent:
        llm = None

        async def aexecute_task(self, task, context=None):
            await asyncio.sleep(1)
            return "too late"

    with pytest.raises(BudgetExceeded, match="time budget"):
        await CrewAIStageExecutor().execute(
            agent_id="researcher",
            agent=SlowAgent(),
            task=SimpleNamespace(id="task", description="research"),
            context=None,
            stage_ctx=stage_ctx,
        )


async def test_crewai_stage_result_usage_charges_shared_guard_once():
    guard = BudgetGuard(_limits(max_total_tokens=10, max_cost_usd=1.0))
    stage_ctx = make_stage_context("metered-stage", budget_guard=guard)

    class MeteredLLM:
        def __init__(self):
            self.total = 0

        def get_token_usage_summary(self):
            return {
                "prompt_tokens": self.total,
                "completion_tokens": 0,
                "total_tokens": self.total,
            }

    class MeteredAgent:
        llm = MeteredLLM()

        async def aexecute_task(self, task, context=None):
            self.llm.total = 10
            return "complete"

    stage_ctx.model_config = SimpleNamespace(model_name="unpriced-test")
    result = await CrewAIStageExecutor().execute(
        agent_id="researcher",
        agent=MeteredAgent(),
        task=SimpleNamespace(id="task", description="research"),
        context=None,
        stage_ctx=stage_ctx,
    )
    stage_ctx.record_usage("stage:researcher", result.usage, model_usage=True)
    stage_ctx.record_usage("stage:researcher", result.usage, model_usage=True)

    assert guard.tokens_used == 10
    with pytest.raises(BudgetExceeded, match="token budget"):
        guard.check()


async def test_failed_crewai_stage_charges_observed_model_usage_and_cost(monkeypatch):
    guard = BudgetGuard(_limits(max_total_tokens=100, max_cost_usd=1.0))
    stage_ctx = make_stage_context("failed-metered-stage", budget_guard=guard)
    stage_ctx.model_config = SimpleNamespace(model_name="priced-test")
    monkeypatch.setattr("app.core.pricing.usage_cost", lambda *_args: 0.4)

    class MeteredLLM:
        def __init__(self):
            self.total = 0

        def get_token_usage_summary(self):
            return {
                "prompt_tokens": self.total,
                "completion_tokens": 0,
                "total_tokens": self.total,
            }

    class FailingAgent:
        llm = MeteredLLM()

        async def aexecute_task(self, task, context=None):
            self.llm.total = 5
            raise RuntimeError("provider failed after metering")

    with pytest.raises(RuntimeError, match="provider failed"):
        await CrewAIStageExecutor().execute(
            agent_id="researcher",
            agent=FailingAgent(),
            task=SimpleNamespace(id="task", description="research"),
            context=None,
            stage_ctx=stage_ctx,
        )

    assert guard.tokens_used == 5
    assert guard.cost_usd_used == pytest.approx(0.4)


async def test_no_summary_stage_result_falls_back_to_output_usage_once():
    guard = BudgetGuard(_limits(max_total_tokens=100, max_cost_usd=1.0))
    stage_ctx = make_stage_context("no-summary-stage", budget_guard=guard)
    stage_ctx.model_config = SimpleNamespace(model_name="unpriced-test")

    class NoSummaryLLM:
        pass

    class Agent:
        llm = NoSummaryLLM()

        async def aexecute_task(self, task, context=None):
            return SimpleNamespace(
                raw="complete",
                usage={"total_tokens": 5, "cost_usd": 0.2},
            )

    result = await CrewAIStageExecutor().execute(
        agent_id="researcher",
        agent=Agent(),
        task=SimpleNamespace(id="task", description="research"),
        context=None,
        stage_ctx=stage_ctx,
    )
    assert result.usage_charged is False
    stage_ctx.record_usage(
        "crewai:stage:researcher", result.usage, model_usage=True
    )
    stage_ctx.record_usage(
        "crewai:stage:researcher", result.usage, model_usage=True
    )

    assert guard.tokens_used == 5
    assert guard.cost_usd_used == pytest.approx(0.2)


async def test_empty_summary_failure_falls_back_to_exception_usage_once():
    guard = BudgetGuard(_limits(max_total_tokens=100, max_cost_usd=1.0))
    stage_ctx = make_stage_context("empty-summary-failure", budget_guard=guard)
    stage_ctx.model_config = SimpleNamespace(model_name="unpriced-test")

    class EmptySummaryLLM:
        def get_token_usage_summary(self):
            return None

    class Agent:
        llm = EmptySummaryLLM()

        async def aexecute_task(self, task, context=None):
            error = RuntimeError("provider failed with output usage")
            error.usage = {"total_tokens": 3, "cost_usd": 0.1}
            raise error

    with pytest.raises(RuntimeError, match="provider failed with output usage"):
        await CrewAIStageExecutor().execute(
            agent_id="researcher",
            agent=Agent(),
            task=SimpleNamespace(id="task", description="research"),
            context=None,
            stage_ctx=stage_ctx,
        )

    assert guard.tokens_used == 3
    assert guard.cost_usd_used == pytest.approx(0.1)


async def test_summary_realtime_charge_is_not_duplicated_by_stage_fallback():
    from app.agents.adapters.llm_adapter import wrap_crewai_llm_with_budget

    guard = BudgetGuard(_limits(max_total_tokens=100, max_cost_usd=1.0))
    stage_ctx = make_stage_context("realtime-summary-stage", budget_guard=guard)
    stage_ctx.model_config = SimpleNamespace(
        model_name="gpt-4o", max_context_tokens=4_000, max_tokens=200
    )

    class SummaryLLM:
        def __init__(self):
            self.total = 0

        def call(self, *args, **kwargs):
            raise AssertionError("sync path is not used")

        async def acall(self, *args, **kwargs):
            self.total += 5
            return "model output"

        def get_token_usage_summary(self):
            return {"total_tokens": self.total, "cost_usd": self.total * 0.04}

    class Agent:
        def __init__(self):
            self.llm = wrap_crewai_llm_with_budget(
                SummaryLLM(), stage_ctx.model_config, budget_guard=guard
            )

        async def aexecute_task(self, task, context=None):
            await self.llm.acall(
                messages=[{"role": "user", "content": "research"}]
            )
            return SimpleNamespace(
                raw="complete",
                usage={"total_tokens": 5, "cost_usd": 0.2},
            )

    result = await CrewAIStageExecutor().execute(
        agent_id="researcher",
        agent=Agent(),
        task=SimpleNamespace(id="task", description="research"),
        context=None,
        stage_ctx=stage_ctx,
    )
    if result.usage and not result.usage_charged:
        stage_ctx.record_usage(
            "crewai:stage:researcher", result.usage, model_usage=True
        )

    assert result.usage_charged is True
    assert guard.tokens_used == 5
    assert guard.cost_usd_used == pytest.approx(0.2)


async def test_single_crewai_failure_charges_partial_cumulative_usage(monkeypatch):
    import crewai

    from app.agents.runtime.crewai_runtime import CrewAIRuntime

    class MeteredLLM:
        total = 0

        def get_token_usage_summary(self):
            return {
                "prompt_tokens": self.total,
                "completion_tokens": 0,
                "total_tokens": self.total,
            }

    class FakeAgent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeTask:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FailingCrew:
        def __init__(self, *, agents, **kwargs):
            self.agent = agents[0]

        async def kickoff_async(self):
            self.agent.llm.total = 6
            raise RuntimeError("failed after provider metered usage")

    monkeypatch.setattr(crewai, "Agent", FakeAgent)
    monkeypatch.setattr(crewai, "Task", FakeTask)
    monkeypatch.setattr(crewai, "Crew", FailingCrew)
    monkeypatch.setattr("app.agents.runtime.crewai_runtime.usage_cost", lambda *_: 0.3)
    guard = BudgetGuard(_limits(max_total_tokens=100, max_cost_usd=1.0))
    ctx = SimpleNamespace(
        enable_tools=False,
        user_content="bounded request",
        assistant_msg=SimpleNamespace(id="message", content="", metadata_={}),
        extra={},
        budget_guard=guard,
        model_config=SimpleNamespace(model_name="priced-test"),
    )

    events = [
        event
        async for event in CrewAIRuntime()._run_single_agent(
            ctx, MeteredLLM(), "chat"
        )
    ]

    error = [event.data for event in events if event.kind == "error"][-1]
    assert error["usage"]["total_tokens"] == 6
    assert ctx.extra["usage"]["total_tokens"] == 6
    assert guard.tokens_used == 6
    assert guard.cost_usd_used == pytest.approx(0.3)


async def test_single_crewai_failure_without_summary_uses_exception_usage(monkeypatch):
    import crewai

    from app.agents.runtime.crewai_runtime import CrewAIRuntime

    class NoSummaryLLM:
        pass

    class FakeAgent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeTask:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FailingCrew:
        def __init__(self, **kwargs):
            pass

        async def kickoff_async(self):
            error = RuntimeError("failed with provider-owned usage")
            error.usage = {"total_tokens": 7, "cost_usd": 0.35}
            raise error

    monkeypatch.setattr(crewai, "Agent", FakeAgent)
    monkeypatch.setattr(crewai, "Task", FakeTask)
    monkeypatch.setattr(crewai, "Crew", FailingCrew)
    guard = BudgetGuard(_limits(max_total_tokens=100, max_cost_usd=1.0))
    ctx = SimpleNamespace(
        enable_tools=False,
        user_content="bounded request",
        assistant_msg=SimpleNamespace(id="message", content="", metadata_={}),
        extra={},
        budget_guard=guard,
        model_config=SimpleNamespace(model_name="unpriced-test"),
    )

    events = [
        event
        async for event in CrewAIRuntime()._run_single_agent(
            ctx, NoSummaryLLM(), "chat"
        )
    ]

    error = [event.data for event in events if event.kind == "error"][-1]
    assert error["usage"]["total_tokens"] == 7
    assert ctx.extra["usage"]["total_tokens"] == 7
    assert guard.tokens_used == 7
    assert guard.cost_usd_used == pytest.approx(0.35)


async def test_single_crewai_result_fallback_does_not_duplicate_realtime_charge(
    monkeypatch,
):
    import crewai

    from app.agents.adapters.llm_adapter import wrap_crewai_llm_with_budget
    from app.agents.runtime.crewai_runtime import CrewAIRuntime

    class FlakySummaryLLM:
        def __init__(self):
            self.total = 0
            self.summary_calls = 0

        def call(self, *args, **kwargs):
            raise AssertionError("sync path is not used")

        async def acall(self, *args, **kwargs):
            self.total = 5
            return "complete"

        def get_token_usage_summary(self):
            self.summary_calls += 1
            # Runtime baseline, wrapper baseline, wrapper final, runtime final.
            if self.summary_calls == 4:
                return None
            return {"total_tokens": self.total, "cost_usd": self.total * 0.04}

    class FakeAgent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeTask:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeCrew:
        def __init__(self, *, agents, **kwargs):
            self.agent = agents[0]

        async def kickoff_async(self):
            await self.agent.llm.acall(
                messages=[{"role": "user", "content": "research"}]
            )
            return SimpleNamespace(
                raw="complete",
                usage={"total_tokens": 5, "cost_usd": 0.2},
            )

    monkeypatch.setattr(crewai, "Agent", FakeAgent)
    monkeypatch.setattr(crewai, "Task", FakeTask)
    monkeypatch.setattr(crewai, "Crew", FakeCrew)
    guard = BudgetGuard(_limits(max_total_tokens=100, max_cost_usd=1.0))
    cfg = SimpleNamespace(
        model_name="gpt-4o", max_context_tokens=4_000, max_tokens=200
    )
    llm = wrap_crewai_llm_with_budget(
        FlakySummaryLLM(), cfg, budget_guard=guard
    )
    ctx = SimpleNamespace(
        enable_tools=False,
        user_content="bounded request",
        assistant_msg=SimpleNamespace(id="message", content="", metadata_={}),
        extra={},
        budget_guard=guard,
        model_config=cfg,
    )

    events = [
        event
        async for event in CrewAIRuntime()._run_single_agent(ctx, llm, "chat")
    ]

    assert not any(event.kind == "error" for event in events)
    assert guard.tokens_used == 5
    assert guard.cost_usd_used == pytest.approx(0.2)
    assert ctx.extra["usage"]["total_tokens"] == 5


async def test_native_runtime_charges_provider_usage_and_stops_explicitly(
    db_session, monkeypatch
):
    from tests.test_native_runtime_graph_events import (
        _collect,
        _FakeProvider,
        _find_all,
        _seed_native_ctx,
    )

    ctx = await _seed_native_ctx(db_session, enable_tools=False)
    ctx.budget_guard = BudgetGuard(
        _limits(max_total_tokens=5, max_cost_usd=1.0)
    )
    provider = _FakeProvider(
        [[
            ChatDelta(content="answer", finish_reason="stop"),
            ChatDelta(
                usage={
                    "prompt_tokens": 4,
                    "completion_tokens": 1,
                    "total_tokens": 5,
                }
            ),
        ]]
    )
    monkeypatch.setattr(
        "app.agents.runtime.native_runtime.get_provider_for_config",
        lambda _cfg: provider,
    )
    monkeypatch.setattr(
        "app.agents.runtime.native_runtime.usage_cost",
        lambda _model, _usage: 0.25,
    )

    events = await _collect(ctx)

    assert len(provider.calls) == 1
    budget_error = _find_all(events, "error")[-1]
    assert budget_error["code"] == "agent_budget_exceeded"
    assert budget_error["finish_reason"] == "budget"
    assert budget_error["budget"] == ctx.extra["budget"]
    done = _find_all(events, "done")[-1]
    assert done["finish_reason"] == "budget"
    assert done["budget"] == ctx.extra["budget"]
    assert ctx.extra["budget"]["used"]["total_tokens"] == 5
    assert ctx.extra["budget"]["used"]["cost_usd"] == pytest.approx(0.25)
    assert ctx.assistant_msg.metadata_["budget"] == ctx.extra["budget"]


async def test_native_runtime_bounds_model_call_by_remaining_wall_clock(
    db_session, monkeypatch
):
    from tests.test_native_runtime_graph_events import (
        _collect,
        _find_all,
        _seed_native_ctx,
    )

    class SlowProvider:
        calls = 0

        async def stream_chat(self, messages, options=None):
            self.calls += 1
            await asyncio.sleep(1)
            yield ChatDelta(content="too late", finish_reason="stop")

    ctx = await _seed_native_ctx(db_session, enable_tools=False)
    ctx.budget_guard = BudgetGuard(_limits(max_runtime_seconds=0.01))
    provider = SlowProvider()
    monkeypatch.setattr(
        "app.agents.runtime.native_runtime.get_provider_for_config",
        lambda _cfg: provider,
    )

    events = await _collect(ctx)

    assert provider.calls == 1
    assert _find_all(events, "error")[-1]["code"] == "agent_budget_exceeded"
    assert _find_all(events, "done")[-1]["finish_reason"] == "budget"


def test_native_tool_formatter_internal_typeerror_is_not_retried():
    from app.agents.runtime.native_runtime import _bounded_tool_message

    class BrokenExecution:
        calls = 0

        def to_openai_tool_message(self, **kwargs):
            self.calls += 1
            raise TypeError("formatter bug")

    execution = BrokenExecution()
    with pytest.raises(TypeError, match="formatter bug"):
        _bounded_tool_message(execution, 32)
    assert execution.calls == 1


async def test_native_runtime_times_out_while_waiting_for_model_limiter(
    db_session, monkeypatch
):
    from tests.test_native_runtime_graph_events import (
        _collect,
        _find_all,
        _seed_native_ctx,
    )

    class NeverAdmittedLimiter:
        async def acquire(self):
            await asyncio.sleep(0.2)

        def release(self):
            raise AssertionError("an unacquired limiter slot must not be released")

    class UncalledProvider:
        async def stream_chat(self, messages, options=None):
            raise AssertionError("provider must not run without a limiter slot")
            yield

    ctx = await _seed_native_ctx(db_session, enable_tools=False)
    ctx.budget_guard = BudgetGuard(_limits(max_runtime_seconds=0.01))
    monkeypatch.setattr(
        "app.agents.runtime.native_runtime.get_provider_for_config",
        lambda _cfg: UncalledProvider(),
    )
    monkeypatch.setattr(
        "app.core.concurrency.model_limiter", lambda: NeverAdmittedLimiter()
    )
    started = time.monotonic()

    events = await _collect(ctx)

    assert time.monotonic() - started < 0.1
    assert _find_all(events, "error")[-1]["code"] == "agent_budget_exceeded"
    assert _find_all(events, "done")[-1]["finish_reason"] == "budget"


async def test_native_runtime_bounds_tool_output_before_followup_model_dispatch(
    db_session, monkeypatch
):
    from tests.test_native_runtime_graph_events import (
        _collect,
        _FakeExecution,
        _FakeProvider,
        _seed_native_ctx,
    )

    class LongResultGateway:
        def __init__(self, *args, **kwargs):
            pass

        async def execute(self, *, tool_call_id, tool_name, arguments):
            return _FakeExecution(
                status="success",
                ok=True,
                approval_id=None,
                result={"content": "x" * 1_000},
                content="x" * 1_000,
                error=None,
                usage=None,
                tool_call_id=tool_call_id,
                name=tool_name,
            )

    ctx = await _seed_native_ctx(db_session, enable_tools=True)
    ctx.budget_guard = BudgetGuard(_limits(max_tool_output_chars=32))
    provider = _FakeProvider(
        [
            [
                ChatDelta(
                    tool_calls=[
                        ToolCallDef(
                            id="long-tool",
                            name="datetime_now",
                            arguments="{}",
                        )
                    ],
                    finish_reason="tool_calls",
                )
            ],
            [ChatDelta(content="done", finish_reason="stop")],
        ]
    )
    monkeypatch.setattr(
        "app.agents.runtime.native_runtime.get_provider_for_config",
        lambda _cfg: provider,
    )
    monkeypatch.setattr(
        "app.agents.runtime.native_runtime.ToolGateway", LongResultGateway
    )

    await _collect(ctx)

    tool_message = provider.calls[1][-1]
    assert tool_message["role"] == "tool"
    assert len(tool_message["content"]) <= 32


async def test_tool_gateway_enforces_per_run_output_limit(db_session):
    from app.agents.gateway.tool_gateway import ToolGateway
    from tests.test_agent_phase0 import _seed_run

    class LongTool(BaseTool):
        name = "long_tool"
        description = "returns a long observation"

        async def run(self, **kwargs):
            return "z" * 100

    registry = ToolRegistry()
    registry.register(LongTool())
    _, msg, run = await _seed_run(db_session)
    gateway = ToolGateway(
        db_session,
        conversation_id=msg.conversation_id,
        assistant_message_id=msg.id,
        run_id=run.id,
        registry=registry,
        max_result_chars=17,
    )

    execution = await gateway.execute(
        tool_call_id="long-1", tool_name="long_tool", arguments={}
    )

    assert execution.truncated is True
    assert len(execution.to_openai_tool_message()["content"]) <= 17
    assert execution.full_result == "z" * 100


def test_crewai_adapter_does_not_reexpand_bounded_gateway_content():
    import json

    from app.agents.adapters.tool_adapter import _format_for_crewai
    from app.agents.schemas import ToolExecution

    execution = ToolExecution(
        ok=True,
        tool_call_id="bounded",
        tool_name="tool",
        arguments={},
        status="success",
        result={"content": "z" * 17, "truncated": True},
        truncated=True,
    )
    rendered = _format_for_crewai(execution, max_chars=17)
    assert len(rendered) <= 17
    assert json.loads(rendered) == "[truncated]"


def test_crewai_adapter_bounds_huge_error_as_valid_json_with_marker():
    import json

    from app.agents.adapters.tool_adapter import _format_for_crewai
    from app.agents.schemas import ToolExecution

    execution = ToolExecution(
        ok=False,
        tool_call_id="failed",
        tool_name="tool",
        arguments={},
        status="error",
        error="failure-detail " * 10_000,
    )

    rendered = _format_for_crewai(execution, max_chars=96)

    assert len(rendered) <= 96
    assert json.loads(rendered)["truncated"] is True
    assert "[truncated]" in rendered


@pytest.mark.parametrize(
    "value",
    [
        "plain unicode 中文 😀",
        {"items": ["quoted \\\" value", "中文"], "ok": True},
        ["a", {"nested": "b"}],
    ],
)
def test_tool_success_observation_is_always_valid_bounded_json(value):
    import json

    from app.agents.adapters.tool_adapter import _format_for_crewai
    from app.agents.schemas import ToolExecution

    execution = ToolExecution(
        ok=True,
        tool_call_id="success",
        tool_name="tool",
        arguments={},
        status="success",
        result=value,
    )
    rendered = _format_for_crewai(execution, max_chars=64)

    assert len(rendered) <= 64
    json.loads(rendered)
    if len(json.dumps(value, ensure_ascii=False)) > 64:
        assert "[truncated]" in rendered


def test_tool_observation_minimum_cap_remains_valid_json():
    import json

    from app.agents.adapters.tool_adapter import _format_for_crewai
    from app.agents.schemas import ToolExecution

    execution = ToolExecution(
        ok=True,
        tool_call_id="small",
        tool_name="tool",
        arguments={},
        status="success",
        result="x" * 1_000,
    )
    rendered = _format_for_crewai(execution, max_chars=16)

    assert len(rendered) <= 16
    assert "[truncated]" in rendered
    json.loads(rendered)


async def test_streaming_writer_gates_blank_retry_as_new_model_dispatch(
    db_session,
):
    from app.agents.runtime.stage_executor import FakeStageExecutor
    from app.agents.streaming_writer import StreamingWriterExecutor
    from tests.test_streaming_writer import _ScriptedWriterProvider, _writer_ctx

    provider = _ScriptedWriterProvider(
        [
            [ChatDelta(content="", finish_reason="stop")],
            [ChatDelta(content="retry", finish_reason="stop")],
        ]
    )
    stage_ctx, _ = await _writer_ctx(db_session, provider)
    stage_ctx.budget_guard = BudgetGuard(_limits(max_agent_steps=1))

    with pytest.raises(BudgetExceeded, match="max agent steps"):
        await StreamingWriterExecutor(FakeStageExecutor()).execute(
            agent_id="writer",
            agent=None,
            task=SimpleNamespace(id="writer", description="write"),
            context="evidence",
            stage_ctx=stage_ctx,
        )

    assert len(provider.calls) == 1


async def test_streaming_writer_timeout_preserves_and_charges_partial_round(db_session):
    from app.agents.runtime.stage_executor import FakeStageExecutor
    from app.agents.streaming_writer import StreamingWriterExecutor
    from tests.test_streaming_writer import _writer_ctx

    class PartialThenSlowProvider:
        async def stream_chat(self, messages, options=None):
            yield ChatDelta(content="partial answer")
            yield ChatDelta(
                usage={
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "total_tokens": 4,
                    "cost_usd": 0.2,
                }
            )
            await asyncio.sleep(1)

    stage_ctx, message = await _writer_ctx(db_session, PartialThenSlowProvider())
    stage_ctx.budget_guard = BudgetGuard(_limits(max_runtime_seconds=0.02))

    with pytest.raises(BudgetExceeded, match="time budget"):
        await StreamingWriterExecutor(FakeStageExecutor()).execute(
            agent_id="writer",
            agent=None,
            task=SimpleNamespace(id="writer", description="write"),
            context="evidence",
            stage_ctx=stage_ctx,
        )

    assert message.content == "partial answer"
    assert stage_ctx.writer_streamed is True
    assert stage_ctx.budget_guard.tokens_used == 4
    assert stage_ctx.budget_guard.cost_usd_used == pytest.approx(0.2)


async def test_crewai_runtime_uses_one_guard_across_all_stages(db_session):
    from app.agents.runtime.stage_executor import FakeStageExecutor
    from tests.test_agent_graph_lifecycle import _collect, _seed_ctx

    ctx = await _seed_ctx(db_session)
    ctx.budget_guard = BudgetGuard(_limits(max_agent_steps=1))
    ctx.extra["stage_executor"] = FakeStageExecutor()

    events = await _collect(ctx)
    errors = [data for kind, data in events if kind == "error"]
    done = [data for kind, data in events if kind == "done"]

    assert errors[-1]["code"] == "agent_budget_exceeded"
    assert done[-1]["finish_reason"] == "budget"
    assert done[-1]["budget"] == ctx.extra["budget"]
    assert ctx.extra["budget"]["used"]["steps"] == 2
    assert ctx.assistant_msg.metadata_["budget"] == ctx.extra["budget"]


async def test_native_tool_await_is_bounded_by_remaining_runtime(
    db_session, monkeypatch
):
    from tests.test_native_runtime_graph_events import (
        _collect,
        _FakeExecution,
        _FakeProvider,
        _find_all,
        _seed_native_ctx,
    )

    class SlowGateway:
        def __init__(self, *args, **kwargs):
            pass

        async def execute(self, *, tool_call_id, tool_name, arguments):
            await asyncio.sleep(1)
            return _FakeExecution(
                status="success",
                ok=True,
                approval_id=None,
                result={"content": "late"},
                error=None,
                usage=None,
                tool_call_id=tool_call_id,
                name=tool_name,
            )

    ctx = await _seed_native_ctx(db_session, enable_tools=True)
    ctx.budget_guard = BudgetGuard(_limits(max_runtime_seconds=0.02))
    provider = _FakeProvider(
        [[
            ChatDelta(
                tool_calls=[
                    ToolCallDef(
                        id="slow-tool",
                        name="datetime_now",
                        arguments="{}",
                    )
                ],
                finish_reason="tool_calls",
            )
        ]]
    )
    monkeypatch.setattr(
        "app.agents.runtime.native_runtime.get_provider_for_config",
        lambda _cfg: provider,
    )
    monkeypatch.setattr("app.agents.runtime.native_runtime.ToolGateway", SlowGateway)

    events = await _collect(ctx)

    assert _find_all(events, "error")[-1]["code"] == "agent_budget_exceeded"
    assert _find_all(events, "done")[-1]["finish_reason"] == "budget"
