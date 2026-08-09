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

from app.agents.runtime.crewai_runtime import CrewAIRuntime
from app.agents.runtime.stage_executor import FakeStageExecutor
from app.agents.schemas import AgentTurnContext, ExecutionMode
from app.agents.stage_context import make_stage_context
from app.agents.token_budget import PromptAdmissionError, calculate_prompt_budget
from app.agents.streaming_writer import StreamingWriterExecutor
from app.model_capabilities import capabilities_from_config
from app.models import AgentRun, Conversation, Message
from app.providers.base import ChatDelta
from app.providers.mock import MockProvider

_SEEDED = uuid.UUID("00000000-0000-0000-0000-000000000001")


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
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id, message_id=msg.id, user_id=_SEEDED,
        runtime="crewai", flow_name="deep_research", status="running",
    )
    db_session.add(run)
    await db_session.flush()
    cfg = SimpleNamespace(
        provider="mock", api_base_url="http://x/v1", api_key_encrypted="",
        model_name="mock", temperature=0.3, top_p=1.0, max_tokens=64, supports_tools=True,
    )
    user = SimpleNamespace(id=_SEEDED, role="user")
    return AgentTurnContext(
        db=db_session, user=user, conversation=conv, model_config=cfg,
        request=SimpleNamespace(), user_content="compare A and B in depth",
        system_prompt="", messages=[], rag_context="", citations=[],
        assistant_msg=msg, run_id=run.id, execution_mode=ExecutionMode.agent,
        agent_profile="deep_research", enable_tools=True,
    )


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
