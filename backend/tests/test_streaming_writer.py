"""StreamingWriterExecutor: the CrewAI Writer streams real token deltas.

Covers the core Phase-1 requirement: the multi-agent final answer must arrive
token-by-token via provider.stream_chat (never a single bulk token event, never
a chunked-string fake), with no duplicate emission and partial content saved on
cancel. Includes an end-to-end run through CrewAIRuntime._run_multi_agent.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.adapters.tool_adapter import _execution_usage, build_crewai_tool
from app.agents.continuation import aggregate_usage
from app.agents.orchestrator import ChatOrchestrator
from app.agents.runtime.crewai_runtime import (
    CrewAIRuntime,
    _aggregate_crewai_usage,
    _writer_usage,
)
from app.agents.runtime.stage_executor import CrewAIStageExecutor, FakeStageExecutor
from app.agents.runtime.stage_executor import StageResult
from app.agents.schemas import (
    AgentTurnContext,
    ExecutionMode,
    ToolExecution,
    ev_done,
    ev_tool_result,
)
from app.agents.stage_context import make_stage_context
from app.agents.token_budget import PromptAdmissionError, calculate_prompt_budget
from app.agents.streaming_writer import StreamingWriterExecutor
from app.model_capabilities import capabilities_from_config
from app.models import AgentRun, Conversation, Message
from app.providers.base import ChatDelta, ProviderError
from app.providers.mock import MockProvider
from app.services.chat_service import ChatService, _persist_continuation_checkpoint
from tests.conftest import TestSessionLocal

_SEEDED = uuid.UUID("00000000-0000-0000-0000-000000000001")


def test_crewai_tool_adapter_extracts_emitted_metering_usage():
    execution = SimpleNamespace(
        usage={"tool_units": 2, "cached_tokens": 3},
        result={"content": "hits", "usage": {"api_key": "must-not-leak"}},
    )
    assert _execution_usage(execution) == {"tool_units": 2, "cached_tokens": 3}


async def test_crewai_tool_adapter_records_gateway_execution_usage_once(monkeypatch):
    async def execute_via_gateway(*args, **kwargs):
        return ToolExecution(
            ok=True,
            tool_call_id="gateway-call",
            tool_name="metered_tool",
            arguments={},
            status="success",
            result={"content": "safe result", "truncated": False},
            full_result='{"answer":"safe result"}',
            usage={"tool_units": 2},
        )

    monkeypatch.setattr(
        "app.agents.adapters.tool_adapter._execute_via_gateway",
        execute_via_gateway,
    )
    stage_ctx = make_stage_context(uuid.uuid4())
    stage_ctx.set_stage(agent_id="researcher", task_id="task-1")
    adapter = build_crewai_tool(
        SimpleNamespace(name="metered_tool", description="metered", parameters=[]),
        conversation_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        run_id=stage_ctx.run_id,
        user_id=None,
        stage_ctx=stage_ctx,
    )

    rendered = await asyncio.to_thread(adapter._run)
    await asyncio.sleep(0.05)

    assert "usage" not in rendered
    assert _aggregate_crewai_usage([], {}, stage_ctx) == {"tool_units": 2}
    tool_events = []
    while not stage_ctx.queue.empty():
        event = stage_ctx.queue.get_nowait()
        if event is not None and event.kind == "tool_result":
            tool_events.append(event)
    assert tool_events[-1].data["usage"] == {"tool_units": 2}


def test_crewai_runtime_extracts_writer_aggregate_usage():
    stages = [SimpleNamespace(agent_id="researcher"), SimpleNamespace(agent_id="writer")]
    outputs = {
        "writer": StageResult(
            agent_id="writer",
            raw="",
            structured={"usage": {"prompt_tokens": 22, "completion_tokens": 5, "total_tokens": 27}},
        )
    }
    assert _writer_usage(stages, outputs) == {
        "prompt_tokens": 22,
        "completion_tokens": 5,
        "total_tokens": 27,
    }


async def test_crewai_stage_executor_captures_all_reported_model_attempt_usage():
    output = SimpleNamespace(
        raw="research",
        token_usage=[
            {"prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 1},
            SimpleNamespace(
                prompt_tokens=8,
                completion_tokens=3,
                total_tokens=11,
                reasoning_tokens=2,
            ),
        ],
    )

    class FakeAgent:
        tools = []

        async def aexecute_task(self, task, context=None):
            return output

    stage_ctx = make_stage_context(uuid.uuid4())
    result = await CrewAIStageExecutor().execute(
        agent_id="researcher",
        agent=FakeAgent(),
        task=SimpleNamespace(id="t", description="research"),
        context=None,
        stage_ctx=stage_ctx,
    )

    assert result.usage == {
        "prompt_tokens": 18,
        "completion_tokens": 5,
        "total_tokens": 23,
        "cached_tokens": 1,
        "reasoning_tokens": 2,
    }


async def test_crewai_stage_executor_uses_async_llm_usage_delta_for_string_output():
    class UsageMetric:
        def __init__(self, prompt_tokens, completion_tokens, cached_tokens=0):
            self.prompt_tokens = prompt_tokens
            self.completion_tokens = completion_tokens
            self.total_tokens = prompt_tokens + completion_tokens
            self.cached_tokens = cached_tokens

    class FakeLLM:
        def __init__(self):
            self.snapshots = iter(
                [UsageMetric(10, 2, 1), UsageMetric(18, 5, 3)]
            )

        async def get_token_usage_summary(self):
            return next(self.snapshots)

    class FakeAgent:
        tools = []
        llm = FakeLLM()

        async def aexecute_task(self, task, context=None):
            return "research as a raw string"

    result = await CrewAIStageExecutor().execute(
        agent_id="researcher",
        agent=FakeAgent(),
        task=SimpleNamespace(id="t", description="research"),
        context=None,
        stage_ctx=make_stage_context(uuid.uuid4()),
    )

    assert result.raw == "research as a raw string"
    assert result.usage == {
        "prompt_tokens": 8,
        "completion_tokens": 3,
        "total_tokens": 11,
        "cached_tokens": 2,
    }


async def test_crewai_stage_executor_deltas_cumulative_llm_usage_across_attempts():
    class FakeLLM:
        def __init__(self):
            self.snapshots = iter(
                [
                    {"prompt_tokens": 0, "completion_tokens": 0},
                    {"prompt_tokens": 10, "completion_tokens": 2},
                    {"prompt_tokens": 10, "completion_tokens": 2},
                    {"prompt_tokens": 16, "completion_tokens": 5},
                ]
            )

        def get_token_usage_summary(self):
            return next(self.snapshots)

    class FakeAgent:
        tools = []
        llm = FakeLLM()

        async def aexecute_task(self, task, context=None):
            return "raw"

    executor = CrewAIStageExecutor()
    stage_ctx = make_stage_context(uuid.uuid4())
    first = await executor.execute(
        agent_id="researcher",
        agent=FakeAgent(),
        task=SimpleNamespace(id="t1", description="research"),
        context=None,
        stage_ctx=stage_ctx,
    )
    second = await executor.execute(
        agent_id="researcher",
        agent=FakeAgent(),
        task=SimpleNamespace(id="t2", description="retry"),
        context=None,
        stage_ctx=stage_ctx,
    )

    assert first.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
    }
    assert second.usage == {
        "prompt_tokens": 6,
        "completion_tokens": 3,
        "total_tokens": 9,
    }
    assert aggregate_usage([first.usage, second.usage]) == {
        "prompt_tokens": 16,
        "completion_tokens": 5,
        "total_tokens": 21,
    }


async def test_crewai_stage_executor_records_failed_llm_usage_delta_in_finally():
    class FakeLLM:
        def __init__(self):
            self.snapshots = iter(
                [
                    {"prompt_tokens": 20, "completion_tokens": 4},
                    {"prompt_tokens": 27, "completion_tokens": 6},
                ]
            )

        def get_token_usage_summary(self):
            return next(self.snapshots)

    class FakeAgent:
        tools = []
        llm = FakeLLM()

        async def aexecute_task(self, task, context=None):
            raise RuntimeError("provider failed after metering")

    stage_ctx = make_stage_context(uuid.uuid4())
    with pytest.raises(RuntimeError, match="provider failed"):
        await CrewAIStageExecutor().execute(
            agent_id="analyst",
            agent=FakeAgent(),
            task=SimpleNamespace(id="t", description="analyse"),
            context=None,
            stage_ctx=stage_ctx,
        )

    assert aggregate_usage(stage_ctx.usage_records.values()) == {
        "prompt_tokens": 7,
        "completion_tokens": 2,
        "total_tokens": 9,
    }


async def test_crewai_parallel_shared_llm_usage_is_allocated_exactly_once():
    class SharedLLM:
        def __init__(self):
            self.prompt_tokens = 0
            self.completion_tokens = 0

        def get_token_usage_summary(self):
            return {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
            }

    class FakeAgent:
        tools = []

        def __init__(self, llm, prompt_tokens, completion_tokens, delay):
            self.llm = llm
            self.prompt_tokens = prompt_tokens
            self.completion_tokens = completion_tokens
            self.delay = delay

        async def aexecute_task(self, task, context=None):
            await asyncio.sleep(self.delay)
            self.llm.prompt_tokens += self.prompt_tokens
            self.llm.completion_tokens += self.completion_tokens
            return task.id

    llm = SharedLLM()
    stage_ctx = make_stage_context(uuid.uuid4())
    executor = CrewAIStageExecutor()
    results = await asyncio.gather(
        executor.execute(
            agent_id="researcher_a",
            agent=FakeAgent(llm, 20, 3, 0.03),
            task=SimpleNamespace(id="a", description="research A"),
            context=None,
            stage_ctx=stage_ctx,
        ),
        executor.execute(
            agent_id="researcher_b",
            agent=FakeAgent(llm, 10, 2, 0.01),
            task=SimpleNamespace(id="b", description="research B"),
            context=None,
            stage_ctx=stage_ctx,
        ),
    )

    assert aggregate_usage(result.usage for result in results) == {
        "prompt_tokens": 30,
        "completion_tokens": 5,
        "total_tokens": 35,
    }


async def test_crewai_parallel_shared_llm_failure_usage_is_allocated_exactly_once():
    class SharedAsyncLLM:
        def __init__(self):
            self.prompt_tokens = 0
            self.completion_tokens = 0

        async def get_token_usage_summary(self):
            await asyncio.sleep(0)
            return {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
            }

    class FakeAgent:
        tools = []

        def __init__(self, llm, prompt_tokens, completion_tokens, delay, fail=False):
            self.llm = llm
            self.prompt_tokens = prompt_tokens
            self.completion_tokens = completion_tokens
            self.delay = delay
            self.fail = fail

        async def aexecute_task(self, task, context=None):
            await asyncio.sleep(self.delay)
            self.llm.prompt_tokens += self.prompt_tokens
            self.llm.completion_tokens += self.completion_tokens
            if self.fail:
                raise RuntimeError("shared llm failed stage")
            return task.id

    llm = SharedAsyncLLM()
    stage_ctx = make_stage_context(uuid.uuid4())
    executor = CrewAIStageExecutor()
    outcomes = await asyncio.gather(
        executor.execute(
            agent_id="researcher_a",
            agent=FakeAgent(llm, 20, 3, 0.03, fail=True),
            task=SimpleNamespace(id="a", description="research A"),
            context=None,
            stage_ctx=stage_ctx,
        ),
        executor.execute(
            agent_id="researcher_b",
            agent=FakeAgent(llm, 10, 2, 0.01),
            task=SimpleNamespace(id="b", description="research B"),
            context=None,
            stage_ctx=stage_ctx,
        ),
        return_exceptions=True,
    )

    successful = [outcome for outcome in outcomes if isinstance(outcome, StageResult)]
    assert len(successful) == 1
    assert aggregate_usage(
        [successful[0].usage, *stage_ctx.usage_records.values()]
    ) == {
        "prompt_tokens": 30,
        "completion_tokens": 5,
        "total_tokens": 35,
    }


async def test_crewai_distinct_llms_are_not_globally_serialized():
    both_running = asyncio.Event()
    running = 0

    class DistinctLLM:
        def __init__(self):
            self.prompt_tokens = 0

        def get_token_usage_summary(self):
            return {"prompt_tokens": self.prompt_tokens}

    class BarrierAgent:
        tools = []

        def __init__(self):
            self.llm = DistinctLLM()

        async def aexecute_task(self, task, context=None):
            nonlocal running
            running += 1
            if running == 2:
                both_running.set()
            await asyncio.wait_for(both_running.wait(), timeout=0.5)
            self.llm.prompt_tokens += 1
            return task.id

    stage_ctx = make_stage_context(uuid.uuid4())
    executor = CrewAIStageExecutor()
    results = await asyncio.gather(
        *[
            executor.execute(
                agent_id=f"researcher_{index}",
                agent=BarrierAgent(),
                task=SimpleNamespace(id=str(index), description="research"),
                context=None,
                stage_ctx=stage_ctx,
            )
            for index in range(2)
        ]
    )

    assert aggregate_usage(result.usage for result in results) == {
        "prompt_tokens": 2,
        "total_tokens": 2,
    }


async def test_crewai_aggregate_includes_every_stage_and_emitted_tool_usage():
    stage_ctx = make_stage_context(uuid.uuid4())
    stage_ctx.emit(
        ev_tool_result(
            id="tool-1",
            name="metered_search",
            ok=True,
            result="ok",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "tool_units": 4},
        )
    )
    stages = [
        SimpleNamespace(agent_id="researcher"),
        SimpleNamespace(agent_id="analyst"),
        SimpleNamespace(agent_id="writer"),
    ]
    outputs = {
        "researcher": StageResult(
            agent_id="researcher",
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        ),
        "analyst": StageResult(
            agent_id="analyst",
            usage={"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
        ),
        "writer": StageResult(
            agent_id="writer",
            usage={"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
        ),
    }

    assert _aggregate_crewai_usage(stages, outputs, stage_ctx) == {
        "prompt_tokens": 31,
        "completion_tokens": 10,
        "total_tokens": 41,
        "tool_units": 4,
    }

def _stage_ctx_with_provider(msg: Message) -> "make_stage_context":  # type: ignore[name-defined]
    stage_ctx = make_stage_context(uuid.uuid4())
    stage_ctx.provider = MockProvider(base_url="http://x/v1", model="mock")
    stage_ctx.assistant_msg = msg
    stage_ctx.user_content = "What is CrewAI?"
    return stage_ctx


async def test_writer_streams_multiple_token_deltas(db_session):
    conv = Conversation(user_id=_SEEDED, title="w")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.commit()

    stage_ctx = _stage_ctx_with_provider(msg)
    task = SimpleNamespace(description="Answer the user's question about CrewAI.", id="t1")
    executor = StreamingWriterExecutor(FakeStageExecutor())

    result = await executor.execute(
        agent_id="writer", agent=None, task=task,
        context="verified: CrewAI is a multi-agent framework.",
        stage_ctx=stage_ctx,
    )
    # Let the thread-safe queue callbacks flush.
    await asyncio.sleep(0.05)

    deltas: list[str] = []
    while not stage_ctx.queue.empty():
        evt = stage_ctx.queue.get_nowait()
        if evt is not None and evt.kind == "token":
            deltas.append(evt.data["delta"])

    # Real streaming -> many deltas (mock yields word-by-word), not one bulk blob.
    assert len(deltas) > 1, f"expected multiple token deltas, got {deltas}"
    joined = "".join(deltas)
    # Content was accumulated incrementally on the message.
    assert msg.content == joined
    assert joined.strip()  # non-empty
    # Signals to the runtime: do NOT re-emit a bulk token / overwrite content.
    assert result.raw == ""
    assert stage_ctx.writer_streamed is True


class _CancelProvider(MockProvider):
    """Yields one delta, then cancels — to exercise partial-content survival."""
    async def stream_chat(self, messages, options=None):
        yield ChatDelta(content="partial answer ")
        raise asyncio.CancelledError()


async def test_writer_cancel_preserves_partial_content(db_session):
    conv = Conversation(user_id=_SEEDED, title="w2")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.commit()

    stage_ctx = make_stage_context(uuid.uuid4())
    stage_ctx.provider = _CancelProvider(base_url="http://x/v1", model="mock")
    stage_ctx.assistant_msg = msg
    stage_ctx.user_content = "q"

    executor = StreamingWriterExecutor(FakeStageExecutor())
    with pytest.raises(asyncio.CancelledError):
        await executor.execute(
            agent_id="writer", agent=None, task=SimpleNamespace(description="q", id="t"),
            context="verified", stage_ctx=stage_ctx,
        )

    assert msg.content == "partial answer "
    assert stage_ctx.writer_streamed is True


class _RecordingProvider(MockProvider):
    """Records the ChatOptions + messages the writer passed to stream_chat."""
    last_options = None
    last_messages = None

    async def stream_chat(self, messages, options=None):
        self.last_options = options
        self.last_messages = messages
        yield ChatDelta(content="ok", finish_reason="stop")


class _LengthProvider(MockProvider):
    """Yields tokens then finish=length, to prove the real reason is recorded."""

    async def stream_chat(self, messages, options=None):
        yield ChatDelta(content="partial code ")
        yield ChatDelta(content="", finish_reason="length")


class _ScriptedWriterProvider(MockProvider):
    def __init__(self, rounds):
        super().__init__(base_url="http://x/v1", model="mock")
        self.rounds = list(rounds)
        self.calls = []

    async def stream_chat(self, messages, options=None):
        self.calls.append(list(messages))
        for delta in self.rounds.pop(0):
            yield delta


async def _writer_ctx(db_session, provider):
    conv = Conversation(user_id=_SEEDED, title="writer continuation")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    stage_ctx = make_stage_context(uuid.uuid4())
    run = AgentRun(
        id=stage_ctx.run_id,
        conversation_id=conv.id,
        message_id=msg.id,
        user_id=_SEEDED,
        runtime="crewai",
        flow_name="deep_research",
        status="running",
    )
    db_session.add(run)
    await db_session.commit()
    stage_ctx.provider = provider
    stage_ctx.db = db_session
    stage_ctx.persistence_session_factory = TestSessionLocal
    stage_ctx.persistence_lock = asyncio.Lock()
    stage_ctx.assistant_msg = msg
    stage_ctx.user_content = "write a complete answer"
    stage_ctx.model_config = SimpleNamespace(max_tokens=4096)
    return stage_ctx, msg


async def _durable_run(run_id):
    async with TestSessionLocal() as session:
        return await session.get(AgentRun, run_id)


async def _writer_tokens(stage_ctx):
    await asyncio.sleep(0.05)
    tokens = []
    while not stage_ctx.queue.empty():
        event = stage_ctx.queue.get_nowait()
        if event is not None and event.kind == "token":
            tokens.append(event.data["delta"])
    return tokens


async def test_writer_auto_continues_length_and_emits_only_novel_tail(db_session):
    provider = _ScriptedWriterProvider(
        [
            [
                ChatDelta(content="alpha beta"),
                ChatDelta(finish_reason="length"),
                ChatDelta(usage={"prompt_tokens": 10, "completion_tokens": 2}),
            ],
            [
                ChatDelta(content="beta gamma"),
                ChatDelta(finish_reason="stop"),
                ChatDelta(usage={"prompt_tokens": 12, "completion_tokens": 3}),
            ],
        ]
    )
    stage_ctx, msg = await _writer_ctx(db_session, provider)

    result = await StreamingWriterExecutor(FakeStageExecutor()).execute(
        agent_id="writer",
        agent=None,
        task=SimpleNamespace(description="q", id="t"),
        context="verified",
        stage_ctx=stage_ctx,
    )

    assert len(provider.calls) == 2
    assert "continue" in provider.calls[1][-1]["content"].lower()
    assert msg.content == "alpha beta gamma"
    assert "".join(await _writer_tokens(stage_ctx)) == "alpha beta gamma"
    assert result.structured["finish_reason"] == "stop"
    assert result.structured["usage"] == {
        "prompt_tokens": 22,
        "completion_tokens": 5,
        "total_tokens": 27,
    }
    assert msg.metadata_["continuation"]["status"] == "completed"
    run = await _durable_run(stage_ctx.run_id)
    assert run.output["continuation"] == result.structured["continuation"]


async def test_writer_persists_checkpoint_before_followup_provider_dispatch(db_session):
    order = []

    class OrderedWriterProvider(MockProvider):
        def __init__(self):
            super().__init__(base_url="http://x/v1", model="mock")
            self.calls = 0

        async def stream_chat(self, messages, options=None):
            self.calls += 1
            order.append(("provider", self.calls))
            if self.calls == 1:
                yield ChatDelta(content="alpha")
                yield ChatDelta(finish_reason="length")
                return
            yield ChatDelta(content=" beta")
            yield ChatDelta(finish_reason="stop")

    async def persist_checkpoint(checkpoint):
        await asyncio.sleep(0)
        order.append(("persist", checkpoint["round"], checkpoint["status"]))

    stage_ctx, _msg = await _writer_ctx(db_session, OrderedWriterProvider())
    stage_ctx.persist_continuation_checkpoint = persist_checkpoint

    await StreamingWriterExecutor(FakeStageExecutor()).execute(
        agent_id="writer",
        agent=None,
        task=SimpleNamespace(description="q", id="t"),
        context="verified",
        stage_ctx=stage_ctx,
    )

    assert order == [
        ("provider", 1),
        ("persist", 1, "continuing"),
        ("provider", 2),
        ("persist", 1, "completed"),
    ]


async def test_writer_cancel_during_continuing_checkpoint_avoids_followup_dispatch(
    db_session,
):
    provider = _ScriptedWriterProvider(
        [
            [ChatDelta(content="alpha"), ChatDelta(finish_reason="length")],
            [ChatDelta(content="must not dispatch"), ChatDelta(finish_reason="stop")],
        ]
    )
    stage_ctx, msg = await _writer_ctx(db_session, provider)
    stage_ctx.cancel_event = asyncio.Event()
    order = []

    async def persist_checkpoint(checkpoint):
        order.append(checkpoint["status"])
        if checkpoint["status"] == "continuing":
            stage_ctx.cancel_event.set()
        await _persist_continuation_checkpoint(
            TestSessionLocal, msg, stage_ctx.run_id, checkpoint
        )

    stage_ctx.persist_continuation_checkpoint = persist_checkpoint

    result = await StreamingWriterExecutor(FakeStageExecutor()).execute(
        agent_id="writer",
        agent=None,
        task=SimpleNamespace(description="q", id="t"),
        context="verified",
        stage_ctx=stage_ctx,
    )

    assert len(provider.calls) == 1
    assert order == ["continuing", "cancelled"]
    assert result.structured["finish_reason"] == "cancelled"
    run = await _durable_run(stage_ctx.run_id)
    assert run.output["continuation"]["status"] == "cancelled"


async def test_writer_task_cancelled_during_checkpoint_replaces_stale_continuing(
    db_session,
):
    provider = _ScriptedWriterProvider(
        [[ChatDelta(content="alpha"), ChatDelta(finish_reason="length")]]
    )
    stage_ctx, msg = await _writer_ctx(db_session, provider)
    order = []

    async def persist_checkpoint(checkpoint):
        order.append(checkpoint["status"])
        await _persist_continuation_checkpoint(
            TestSessionLocal, msg, stage_ctx.run_id, checkpoint
        )
        if checkpoint["status"] == "continuing":
            raise asyncio.CancelledError()

    stage_ctx.persist_continuation_checkpoint = persist_checkpoint

    with pytest.raises(asyncio.CancelledError):
        await StreamingWriterExecutor(FakeStageExecutor()).execute(
            agent_id="writer",
            agent=None,
            task=SimpleNamespace(description="q", id="t"),
            context="verified",
            stage_ctx=stage_ctx,
        )

    assert len(provider.calls) == 1
    assert order == ["continuing", "cancelled"]
    run = await _durable_run(stage_ctx.run_id)
    assert run.output["continuation"]["status"] == "cancelled"


@pytest.mark.parametrize(
    "raised",
    [asyncio.CancelledError(), RuntimeError("checkpoint unavailable")],
)
async def test_writer_checkpoint_failure_records_completed_round_usage_once(
    db_session, raised
):
    provider = _ScriptedWriterProvider(
        [
            [
                ChatDelta(content="partial answer"),
                ChatDelta(usage={"prompt_tokens": 10, "completion_tokens": 2}),
                ChatDelta(finish_reason="length"),
            ]
        ]
    )
    stage_ctx, msg = await _writer_ctx(db_session, provider)

    async def persist_checkpoint(checkpoint):
        if checkpoint["status"] == "continuing":
            raise raised
        await _persist_continuation_checkpoint(
            TestSessionLocal, msg, stage_ctx.run_id, checkpoint
        )

    stage_ctx.persist_continuation_checkpoint = persist_checkpoint

    with pytest.raises(type(raised)):
        await StreamingWriterExecutor(FakeStageExecutor()).execute(
            agent_id="writer",
            agent=None,
            task=SimpleNamespace(description="q", id="t"),
            context="verified",
            stage_ctx=stage_ctx,
        )

    assert len(provider.calls) == 1
    assert msg.content == "partial answer"
    assert aggregate_usage(stage_ctx.usage_records.values()) == {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
    }
    assert len(stage_ctx.usage_records) == 1
    run = await _durable_run(stage_ctx.run_id)
    if isinstance(raised, asyncio.CancelledError):
        assert run.output["continuation"]["status"] == "cancelled"
    else:
        assert run.output is None


async def test_writer_stops_at_max_continuation_rounds(db_session):
    provider = _ScriptedWriterProvider(
        [
            [ChatDelta(content="A"), ChatDelta(finish_reason="length")],
            [ChatDelta(content="A B"), ChatDelta(finish_reason="length")],
            [ChatDelta(content="B C"), ChatDelta(finish_reason="length")],
        ]
    )
    stage_ctx, msg = await _writer_ctx(db_session, provider)

    result = await StreamingWriterExecutor(FakeStageExecutor()).execute(
        agent_id="writer",
        agent=None,
        task=SimpleNamespace(description="q", id="t"),
        context="verified",
        stage_ctx=stage_ctx,
    )

    assert len(provider.calls) == 3
    assert msg.content == "A B C"
    assert result.structured["finish_reason"] == "length"
    assert result.structured["continuation"]["status"] == "maxed"
    run = await _durable_run(stage_ctx.run_id)
    assert run.output["continuation"] == result.structured["continuation"]


async def test_writer_usage_includes_empty_stream_retry(db_session):
    provider = _ScriptedWriterProvider(
        [
            [
                ChatDelta(finish_reason="stop"),
                ChatDelta(usage={"prompt_tokens": 5, "completion_tokens": 0}),
            ],
            [
                ChatDelta(content="answer", finish_reason="stop"),
                ChatDelta(usage={"prompt_tokens": 6, "completion_tokens": 1}),
            ],
        ]
    )
    stage_ctx, _msg = await _writer_ctx(db_session, provider)

    result = await StreamingWriterExecutor(FakeStageExecutor()).execute(
        agent_id="writer",
        agent=None,
        task=SimpleNamespace(description="q", id="t"),
        context="verified",
        stage_ctx=stage_ctx,
    )

    assert result.structured["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 1,
        "total_tokens": 12,
    }


async def test_writer_direct_provider_usage_does_not_read_agent_llm_summary(db_session):
    provider = _ScriptedWriterProvider(
        [
            [
                ChatDelta(content="answer", finish_reason="stop"),
                ChatDelta(usage={"prompt_tokens": 6, "completion_tokens": 2}),
            ]
        ]
    )
    stage_ctx, _msg = await _writer_ctx(db_session, provider)

    class UnrelatedCrewLLM:
        calls = 0

        def get_token_usage_summary(self):
            self.calls += 1
            return {"prompt_tokens": 100, "completion_tokens": 50}

    llm = UnrelatedCrewLLM()
    result = await StreamingWriterExecutor(CrewAIStageExecutor()).execute(
        agent_id="writer",
        agent=SimpleNamespace(llm=llm),
        task=SimpleNamespace(description="q", id="t"),
        context="verified",
        stage_ctx=stage_ctx,
    )

    assert result.usage == {
        "prompt_tokens": 6,
        "completion_tokens": 2,
        "total_tokens": 8,
    }
    assert llm.calls == 0


async def test_writer_provider_error_during_continuation_preserves_novel_partial(
    db_session,
):
    class ErroringContinuationProvider(MockProvider):
        def __init__(self):
            super().__init__(base_url="http://x/v1", model="mock")
            self.calls = 0

        async def stream_chat(self, messages, options=None):
            self.calls += 1
            if self.calls == 1:
                yield ChatDelta(content="alpha")
                yield ChatDelta(finish_reason="length")
                return
            yield ChatDelta(content="alpha beta")
            raise ProviderError("connection lost")

    stage_ctx, msg = await _writer_ctx(db_session, ErroringContinuationProvider())

    with pytest.raises(ProviderError):
        await StreamingWriterExecutor(FakeStageExecutor()).execute(
            agent_id="writer",
            agent=None,
            task=SimpleNamespace(description="q", id="t"),
            context="verified",
            stage_ctx=stage_ctx,
        )

    assert msg.content == "alpha beta"
    assert "".join(await _writer_tokens(stage_ctx)) == "alpha beta"


@pytest.mark.parametrize(
    "raised",
    [ProviderError("lost"), RuntimeError("boom"), asyncio.CancelledError()],
)
async def test_writer_failed_continuation_preserves_partial_and_all_usage(
    db_session, raised
):
    class FailedContinuationProvider(MockProvider):
        def __init__(self):
            super().__init__(base_url="http://x/v1", model="mock")
            self.calls = 0

        async def stream_chat(self, messages, options=None):
            self.calls += 1
            if self.calls == 1:
                yield ChatDelta(content="alpha")
                yield ChatDelta(usage={"prompt_tokens": 10, "completion_tokens": 1})
                yield ChatDelta(finish_reason="length")
                return
            yield ChatDelta(content="alpha beta")
            yield ChatDelta(usage={"prompt_tokens": 12, "completion_tokens": 2})
            raise raised

    stage_ctx, msg = await _writer_ctx(db_session, FailedContinuationProvider())

    with pytest.raises(type(raised)):
        await StreamingWriterExecutor(FakeStageExecutor()).execute(
            agent_id="writer",
            agent=None,
            task=SimpleNamespace(description="q", id="t"),
            context="verified",
            stage_ctx=stage_ctx,
        )

    assert msg.content == "alpha beta"
    assert aggregate_usage(stage_ctx.usage_records.values()) == {
        "prompt_tokens": 22,
        "completion_tokens": 3,
        "total_tokens": 25,
    }
    if isinstance(raised, asyncio.CancelledError):
        run = await _durable_run(stage_ctx.run_id)
        assert msg.metadata_["continuation"]["status"] == "cancelled"
        assert run.output["continuation"] == msg.metadata_["continuation"]


async def test_writer_cancel_before_continuation_avoids_followup_dispatch(db_session):
    class CancelBeforeFollowupProvider(MockProvider):
        def __init__(self):
            super().__init__(base_url="http://x/v1", model="mock")
            self.calls = 0
            self.cancel_event = None

        async def stream_chat(self, messages, options=None):
            self.calls += 1
            yield ChatDelta(content="partial")
            yield ChatDelta(finish_reason="length")
            self.cancel_event.set()

    provider = CancelBeforeFollowupProvider()
    stage_ctx, msg = await _writer_ctx(db_session, provider)
    stage_ctx.cancel_event = asyncio.Event()
    provider.cancel_event = stage_ctx.cancel_event

    result = await StreamingWriterExecutor(FakeStageExecutor()).execute(
        agent_id="writer",
        agent=None,
        task=SimpleNamespace(description="q", id="t"),
        context="verified",
        stage_ctx=stage_ctx,
    )

    assert provider.calls == 1
    assert msg.content == "partial"
    assert result.structured["finish_reason"] == "cancelled"
    assert result.structured["continuation"]["status"] == "cancelled"
    run = await _durable_run(stage_ctx.run_id)
    assert run.output["continuation"] == result.structured["continuation"]


async def test_writer_terminal_checkpoint_failure_is_not_silently_ignored(db_session):
    provider = _ScriptedWriterProvider(
        [
            [ChatDelta(content="A"), ChatDelta(finish_reason="length")],
            [ChatDelta(content="A B"), ChatDelta(finish_reason="stop")],
        ]
    )
    stage_ctx, _msg = await _writer_ctx(db_session, provider)

    async def persist_checkpoint(checkpoint):
        if checkpoint["status"] != "continuing":
            raise RuntimeError("terminal checkpoint failed")

    stage_ctx.persist_continuation_checkpoint = persist_checkpoint

    with pytest.raises(RuntimeError, match="terminal checkpoint failed"):
        await StreamingWriterExecutor(FakeStageExecutor()).execute(
            agent_id="writer",
            agent=None,
            task=SimpleNamespace(description="q", id="t"),
            context="verified",
            stage_ctx=stage_ctx,
        )


async def test_writer_uses_configured_output_limit_and_parameter(db_session):
    """The direct writer dispatch obeys the canonical model contract."""
    conv = Conversation(user_id=_SEEDED, title="mt")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.commit()

    stage_ctx = make_stage_context(uuid.uuid4())
    rec = _RecordingProvider(base_url="http://x/v1", model="mock")
    stage_ctx.provider = rec
    stage_ctx.assistant_msg = msg
    stage_ctx.user_content = "write a snake game in python"
    stage_ctx.model_config = SimpleNamespace(
        max_context_tokens=8_192,
        max_tokens=1_024,
        output_token_parameter="max_completion_tokens",
    )

    executor = StreamingWriterExecutor(FakeStageExecutor())
    await executor.execute(
        agent_id="writer", agent=None, task=SimpleNamespace(description="q", id="t"),
        context="verified", stage_ctx=stage_ctx,
    )
    await asyncio.sleep(0.05)

    assert rec.last_options is not None, "writer did not call stream_chat"
    assert rec.last_options.max_tokens == 1_024
    assert rec.last_options.output_token_parameter == "max_completion_tokens"


async def test_writer_compacts_oversized_dependency_context_before_dispatch(db_session):
    conv = Conversation(user_id=_SEEDED, title="writer admission")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.commit()

    stage_ctx = make_stage_context(uuid.uuid4())
    rec = _RecordingProvider(base_url="http://x/v1", model="mock")
    stage_ctx.provider = rec
    stage_ctx.assistant_msg = msg
    stage_ctx.user_content = "Summarize the evidence"
    stage_ctx.model_config = SimpleNamespace(
        max_context_tokens=2_000,
        max_tokens=200,
        output_token_parameter="max_tokens",
    )
    original = "dependency-evidence " * 2_000

    await StreamingWriterExecutor(FakeStageExecutor()).execute(
        agent_id="writer",
        agent=None,
        task=SimpleNamespace(description="write final answer", id="t"),
        context=original,
        stage_ctx=stage_ctx,
    )

    assert rec.last_messages is not None
    user_prompt = next(
        message["content"] for message in rec.last_messages if message["role"] == "user"
    )
    assert len(user_prompt) < len(original)
    assert "truncated" in user_prompt.lower()
    budget = calculate_prompt_budget(
        capabilities_from_config(stage_ctx.model_config),
        requested_output=200,
        tool_schema_tokens=512,
    )
    dispatched_chars = sum(
        len(message["content"]) for message in rec.last_messages
    ) + len("write final answer")
    assert dispatched_chars <= budget.input_tokens


async def test_writer_rejects_oversized_fixed_prompt_before_provider_dispatch(
    db_session, monkeypatch
):
    conv = Conversation(user_id=_SEEDED, title="writer rejection")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.commit()

    stage_ctx = make_stage_context(uuid.uuid4())
    rec = _RecordingProvider(base_url="http://x/v1", model="mock")
    stage_ctx.provider = rec
    stage_ctx.assistant_msg = msg
    stage_ctx.user_content = "question"
    stage_ctx.model_config = SimpleNamespace(
        max_context_tokens=1_000,
        max_tokens=200,
        output_token_parameter="max_tokens",
    )
    monkeypatch.setattr("app.agents.streaming_writer._SYSTEM", "system " * 2_000)

    with pytest.raises(PromptAdmissionError) as exc_info:
        await StreamingWriterExecutor(FakeStageExecutor()).execute(
            agent_id="writer",
            agent=None,
            task=SimpleNamespace(description="write final answer", id="t"),
            context="verified evidence",
            stage_ctx=stage_ctx,
        )

    assert exc_info.value.code == "message_too_large"
    assert rec.last_options is None


async def test_writer_prompt_follows_code_intent(db_session):
    """Code intent: the Writer must NOT frame the answer as 'base it on the
    verified content' (that made it echo the Analyst's architecture prose). The
    user prompt directs the model to write complete code, with research as
    optional reference only."""
    conv = Conversation(user_id=_SEEDED, title="intent-code")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.commit()

    stage_ctx = make_stage_context(uuid.uuid4())
    rec = _RecordingProvider(base_url="http://x/v1", model="mock")
    stage_ctx.provider = rec
    stage_ctx.assistant_msg = msg
    stage_ctx.user_content = "用 Python 写一个贪吃蛇游戏"
    stage_ctx.model_config = SimpleNamespace(max_tokens=1024)

    executor = StreamingWriterExecutor(FakeStageExecutor())
    await executor.execute(
        agent_id="writer", agent=None, task=SimpleNamespace(description="q", id="t"),
        context="架构师负责设计，开发者负责编码", stage_ctx=stage_ctx,
    )
    await asyncio.sleep(0.05)

    user_msg = next(m["content"] for m in rec.last_messages if m["role"] == "user")
    # Must NOT use the research framing that biases echoing architecture prose.
    assert "基于以上已验证内容生成最终回答" not in user_msg
    # Must direct the model to produce complete, runnable code.
    assert "完整、可运行" in user_msg


async def test_writer_prompt_keeps_cited_research_for_research_intent(db_session):
    """Research intent: the answer is still built from verified evidence with
    source citations — the code-path change must not regress research answers."""
    conv = Conversation(user_id=_SEEDED, title="intent-research")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.commit()

    stage_ctx = make_stage_context(uuid.uuid4())
    rec = _RecordingProvider(base_url="http://x/v1", model="mock")
    stage_ctx.provider = rec
    stage_ctx.assistant_msg = msg
    stage_ctx.user_content = "请深入调研大模型微调的主流方法并对比"
    stage_ctx.model_config = SimpleNamespace(max_tokens=1024)

    executor = StreamingWriterExecutor(FakeStageExecutor())
    await executor.execute(
        agent_id="writer", agent=None, task=SimpleNamespace(description="q", id="t"),
        context="LoRA 与 QLoRA 是主流参数高效微调方法 [source 1]",
        stage_ctx=stage_ctx,
    )
    await asyncio.sleep(0.05)

    user_msg = next(m["content"] for m in rec.last_messages if m["role"] == "user")
    assert "已验证内容" in user_msg
    assert "来源编号" in user_msg


async def test_writer_records_real_finish_reason_length(db_session):
    """The writer records the upstream finish_reason in StageResult.structured
    (so the runtime no longer hard-codes 'stop' over a real 'length')."""
    conv = Conversation(user_id=_SEEDED, title="fr")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.commit()

    stage_ctx = make_stage_context(uuid.uuid4())
    stage_ctx.provider = _LengthProvider(base_url="http://x/v1", model="mock")
    stage_ctx.db = db_session
    stage_ctx.persistence_session_factory = TestSessionLocal
    stage_ctx.persistence_lock = asyncio.Lock()
    stage_ctx.assistant_msg = msg
    stage_ctx.user_content = "write code"
    stage_ctx.model_config = SimpleNamespace(max_tokens=4096)

    executor = StreamingWriterExecutor(FakeStageExecutor())
    result = await executor.execute(
        agent_id="writer", agent=None, task=SimpleNamespace(description="q", id="t"),
        context="verified", stage_ctx=stage_ctx,
    )
    await asyncio.sleep(0.05)

    assert result.structured["finish_reason"] == "length", (
        f"writer must record the real upstream finish_reason; got "
        f"{result.structured.get('finish_reason')}"
    )
    assert msg.content == "partial code "


# --------------------------------------------------------------------------- #
# End-to-end: the full multi-agent run streams the writer and emits no duplicate.
# --------------------------------------------------------------------------- #
async def _seed_ctx(db_session) -> AgentTurnContext:
    conv = Conversation(user_id=_SEEDED, title="stream e2e")
    db_session.add(conv)
    await db_session.commit()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id, message_id=msg.id, user_id=_SEEDED,
        runtime="crewai", flow_name="deep_research", status="running",
    )
    db_session.add(run)
    await db_session.commit()
    cfg = SimpleNamespace(
        provider="mock", api_base_url="http://x/v1", api_key_encrypted="",
        model_name="mock", temperature=0.3, top_p=1.0, max_tokens=64, supports_tools=True,
    )
    user = SimpleNamespace(id=_SEEDED, role="user")
    ctx = AgentTurnContext(
        db=db_session, user=user, conversation=conv, model_config=cfg,
        request=SimpleNamespace(), user_content="compare A and B in depth",
        system_prompt="", messages=[], rag_context="", citations=[],
        assistant_msg=msg, run_id=run.id, execution_mode=ExecutionMode.agent,
        agent_profile="deep_research", enable_tools=True,
    )
    ctx.extra["persistence_session_factory"] = TestSessionLocal
    ctx.extra["persistence_lock"] = asyncio.Lock()
    return ctx


async def test_graph_failure_rolls_back_only_independent_session(db_session):
    class FailFirstGraphCommitSession(AsyncSession):
        fail_next_commit = True
        commit_calls = 0
        rollback_calls = 0

        async def commit(self):
            type(self).commit_calls += 1
            assert self.in_transaction()
            await self.flush()
            if type(self).fail_next_commit:
                type(self).fail_next_commit = False
                raise SQLAlchemyError("graph commit failed")
            await super().commit()

        async def rollback(self):
            type(self).rollback_calls += 1
            await super().rollback()

    ctx = await _seed_ctx(db_session)
    await db_session.commit()
    run = await db_session.get(AgentRun, ctx.run_id)
    graph_sessions = async_sessionmaker(
        bind=db_session.bind,
        class_=FailFirstGraphCommitSession,
        expire_on_commit=False,
        autoflush=False,
    )
    ctx.extra["persistence_session_factory"] = graph_sessions
    snapshot = {"nodes": [{"id": "writer", "status": "running"}], "edges": []}
    emitter = SimpleNamespace(snapshot=lambda: snapshot)

    # Graph persistence is best-effort for ordinary SQLAlchemy failures.
    await CrewAIRuntime()._persist_graph(ctx, emitter, definition=True)

    assert FailFirstGraphCommitSession.rollback_calls == 1
    # A rollback on ctx.db would expire both and raise MissingGreenlet here.
    assert ctx.assistant_msg.content == ""
    assert run.output is None

    await CrewAIRuntime()._persist_graph(ctx, emitter, definition=True)
    async with graph_sessions() as verify:
        durable_run = await verify.get(AgentRun, ctx.run_id)
        assert durable_run.graph_definition == snapshot
        assert durable_run.graph_state == snapshot


async def test_crewai_writer_checkpoint_cancellation_never_emits_false_success(
    db_session, monkeypatch
):
    ctx = await _seed_ctx(db_session)
    provider = _ScriptedWriterProvider(
        [[
            ChatDelta(content="partial answer"),
            ChatDelta(usage={"prompt_tokens": 10, "completion_tokens": 2}),
            ChatDelta(finish_reason="length"),
        ]]
    )
    monkeypatch.setattr(
        "app.agents.runtime.crewai_runtime.get_provider_for_config",
        lambda _cfg: provider,
    )
    ctx.extra["stage_executor"] = StreamingWriterExecutor(
        FakeStageExecutor(
            behaviors={
                "researcher": FakeStageExecutor.Behavior(output="evidence"),
                "analyst": FakeStageExecutor.Behavior(output="verified findings"),
            }
        )
    )
    checkpoint_statuses = []

    async def persist_checkpoint(checkpoint):
        checkpoint_statuses.append(checkpoint["status"])
        async with ctx.extra["persistence_lock"]:
            await _persist_continuation_checkpoint(
                TestSessionLocal, ctx.assistant_msg, ctx.run_id, checkpoint
            )
        if checkpoint["status"] == "continuing":
            raise asyncio.CancelledError()

    ctx.extra["persist_continuation_checkpoint"] = persist_checkpoint
    events = []

    with pytest.raises(asyncio.CancelledError):
        # First CrewAI import/build is intentionally lazy and can take several
        # seconds on Windows; this bound detects deadlock without timing setup.
        async with asyncio.timeout(15):
            async for event in CrewAIRuntime().stream_turn(ctx):
                events.append(event)

    assert len(provider.calls) == 1
    assert checkpoint_statuses == ["continuing", "cancelled"]
    assert ctx.assistant_msg.content == "partial answer"
    assert ctx.extra["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
    }
    assert not any(event.kind == "done" for event in events)
    assert any(
        event.kind == "run_status" and event.data.get("status") == "cancelled"
        for event in events
    )
    request_run = await db_session.get(AgentRun, ctx.run_id)
    await ChatOrchestrator()._finalize_run(
        db_session,
        request_run,
        ev_done(
            message_id=ctx.assistant_msg.id,
            finish_reason="cancelled",
            usage=ctx.extra["usage"],
        ),
        session_factory=TestSessionLocal,
    )
    await ChatService()._finalize_interrupted(
        db_session,
        ctx.assistant_msg,
        finish_reason="cancelled",
        usage=ctx.extra["usage"],
        model_name="mock",
    )
    async with TestSessionLocal() as verify:
        durable_run = await verify.get(AgentRun, ctx.run_id)
        durable_message = await verify.get(Message, ctx.assistant_msg.id)
        assert durable_run.status == "cancelled"
        assert durable_run.output["continuation"]["status"] == "cancelled"
        assert durable_run.output["usage"] == ctx.extra["usage"]
        assert durable_message.content == "partial answer"
        assert durable_message.metadata_["status"] == "cancelled"
        assert durable_message.total_tokens == 12


async def test_multi_agent_writer_streams_without_duplicate(db_session):
    ctx = await _seed_ctx(db_session)
    # Researcher/analyst use the fake; the writer is streamed by the wrapper.
    ctx.extra["stage_executor"] = StreamingWriterExecutor(FakeStageExecutor(behaviors={
        "researcher": FakeStageExecutor.Behavior(delay=0.02, output="evidence A and B"),
        "analyst": FakeStageExecutor.Behavior(delay=0.02, output="verified findings"),
    }))

    events: list[tuple[str, dict]] = []
    async for evt in CrewAIRuntime().stream_turn(ctx):
        events.append((evt.kind, evt.data))

    kinds = [k for k, _ in events]
    token_deltas = [d["delta"] for k, d in events if k == "token"]

    # Streamed writer produced multiple tokens, not a single bulk event.
    assert len(token_deltas) > 1, f"expected streamed tokens, got {token_deltas}"
    # Run terminated cleanly.
    assert kinds[-1] == "done"
    # The streamed content equals the message content exactly — i.e. no extra
    # bulk re-emission duplicated it.
    assert ctx.assistant_msg.content == "".join(token_deltas)
    assert ctx.assistant_msg.content.strip()


async def test_crewai_terminal_error_carries_all_completed_and_failed_stage_usage(
    db_session,
):
    ctx = await _seed_ctx(db_session)

    class UsageThenFailureExecutor:
        async def execute(self, *, agent_id, agent, task, context, stage_ctx):
            if agent_id == "writer":
                stage_ctx.record_usage(
                    "model:writer:0",
                    {"prompt_tokens": 7, "completion_tokens": 1, "reasoning_tokens": 1},
                )
                raise RuntimeError("writer failed")
            return StageResult(
                agent_id=agent_id,
                raw=f"{agent_id} output",
                usage={"prompt_tokens": 5, "completion_tokens": 2},
            )

    ctx.extra["stage_executor"] = UsageThenFailureExecutor()
    events = []
    async for event in CrewAIRuntime().stream_turn(ctx):
        events.append(event)

    error = [event.data for event in events if event.kind == "error"][-1]
    assert error["usage"] == {
        "prompt_tokens": 17,
        "completion_tokens": 5,
        "total_tokens": 22,
        "reasoning_tokens": 1,
    }
    assert ctx.extra["usage"] == error["usage"]


async def test_crewai_done_aggregates_researcher_analyst_and_writer_usage(db_session):
    ctx = await _seed_ctx(db_session)

    class MeteredExecutor:
        async def execute(self, *, agent_id, agent, task, context, stage_ctx):
            usage_by_agent = {
                "researcher": {"prompt_tokens": 5, "completion_tokens": 2},
                "analyst": {"prompt_tokens": 6, "completion_tokens": 3},
                "writer": {
                    "prompt_tokens": 7,
                    "completion_tokens": 4,
                    "cached_tokens": 2,
                },
            }
            return StageResult(
                agent_id=agent_id,
                raw=f"{agent_id} output",
                usage=usage_by_agent[agent_id],
            )

    ctx.extra["stage_executor"] = MeteredExecutor()
    events = []
    async for event in CrewAIRuntime().stream_turn(ctx):
        events.append(event)

    done = [event.data for event in events if event.kind == "done"][-1]
    assert done["usage"] == {
        "prompt_tokens": 18,
        "completion_tokens": 9,
        "total_tokens": 27,
        "cached_tokens": 2,
    }


async def test_writer_admission_error_becomes_controlled_runtime_failure(
    db_session, monkeypatch
):
    ctx = await _seed_ctx(db_session)
    ctx.model_config.max_context_tokens = 1_000
    ctx.extra["stage_executor"] = StreamingWriterExecutor(
        FakeStageExecutor(
            behaviors={
                "researcher": FakeStageExecutor.Behavior(output="evidence"),
                "analyst": FakeStageExecutor.Behavior(output="verified findings"),
            }
        )
    )
    monkeypatch.setattr("app.agents.streaming_writer._SYSTEM", "system " * 2_000)

    events: list[tuple[str, dict]] = []
    async for evt in CrewAIRuntime().stream_turn(ctx):
        events.append((evt.kind, evt.data))

    errors = [data for kind, data in events if kind == "error"]
    assert errors[-1]["code"] == "message_too_large"
    assert "fixed prompt" in errors[-1]["message"]
    assert ctx.extra["finish_reason"] == "budget"
    assert any(
        kind == "run_status" and data["status"] == "failed"
        for kind, data in events
    )
    assert not any(kind == "done" for kind, _data in events)
