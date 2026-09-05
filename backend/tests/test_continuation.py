from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.continuation import (
    ContinuationBuffer,
    ContinuationPolicy,
    aggregate_usage,
    merge_continuation,
)
from app.agents.orchestrator import ChatOrchestrator
from app.agents.schemas import ev_done, ev_tool_result
from app.core.config import Settings
from app.core.pricing import reset_pricing_cache
from app.models import AgentRun, Conversation, Message
from app.services.chat_service import (
    ChatService,
    _apply_usage_accounting,
    _persist_continuation_checkpoint,
)
from tests.conftest import TestSessionLocal


@pytest.mark.parametrize(
    ("existing", "continuation", "expected"),
    [
        ("alpha beta", "alpha beta", "alpha beta"),
        ("alpha beta gamma", "beta gamma delta", "alpha beta gamma delta"),
        ("alpha", " beta", "alpha beta"),
        ("first line\nsecond line\n", " second line\nthird line", "first line\nsecond line\nthird line"),
        ("\u7b2c\u4e00\u6bb5\u3002\u7b2c\u4e8c\u6bb5\u3002", "\u7b2c\u4e8c\u6bb5\u3002\u7b2c\u4e09\u6bb5\u3002", "\u7b2c\u4e00\u6bb5\u3002\u7b2c\u4e8c\u6bb5\u3002\u7b2c\u4e09\u6bb5\u3002"),
        ("hello", "\n\nworld", "hello\n\nworld"),
    ],
)
def test_merge_continuation_removes_only_repeated_overlap(existing, continuation, expected):
    assert merge_continuation(existing, continuation) == expected


def test_merge_continuation_bounds_overlap_work():
    existing = "x" * 500_000 + "tail"
    continuation = "y" * 500_000 + "novel"

    started = time.perf_counter()
    merged = merge_continuation(existing, continuation, comparison_window=4_096)
    elapsed = time.perf_counter() - started

    assert merged == existing + continuation
    assert elapsed < 0.5


def test_merge_continuation_keeps_full_window_overlap_before_novel_tail():
    overlap = "z" * 4_096
    assert merge_continuation("prefix" + overlap, overlap + "tail") == (
        "prefix" + overlap + "tail"
    )


def test_continuation_policy_is_immutable_and_validates_bounds():
    assert ContinuationPolicy().max_rounds == 2
    with pytest.raises((AttributeError, TypeError)):
        ContinuationPolicy().max_rounds = 3  # type: ignore[misc]
    with pytest.raises(ValueError):
        ContinuationPolicy(max_rounds=-1)
    with pytest.raises(ValueError):
        ContinuationPolicy(max_rounds=9)


def test_tool_result_event_keeps_only_safe_numeric_usage():
    event = ev_tool_result(
        id="tool-1",
        name="metered",
        ok=True,
        result="safe",
        usage={
            "tool_units": 2,
            "cached_tokens": 3.5,
            "api_key": "must-not-leak",
            "negative": -1,
            "not_finite": float("inf"),
            "nested": {"secret": "must-not-leak"},
        },
    )

    assert event.data["usage"] == {"tool_units": 2, "cached_tokens": 3.5}


def test_settings_configures_continuation_rounds_with_same_bounds():
    assert Settings(AUTO_CONTINUATION_MAX_ROUNDS=3).AUTO_CONTINUATION_MAX_ROUNDS == 3
    with pytest.raises(ValidationError):
        Settings(AUTO_CONTINUATION_MAX_ROUNDS=-1)
    with pytest.raises(ValidationError):
        Settings(AUTO_CONTINUATION_MAX_ROUNDS=9)


@pytest.mark.parametrize(
    ("finish_reason", "round_number", "pending_tools", "cancelled", "expected"),
    [
        ("length", 0, False, False, True),
        ("length", 1, False, False, True),
        ("length", 2, False, False, False),
        ("stop", 0, False, False, False),
        ("length", 0, True, False, False),
        ("length", 0, False, True, False),
    ],
)
def test_continuation_policy_decides_only_safe_length_followups(
    finish_reason, round_number, pending_tools, cancelled, expected
):
    policy = ContinuationPolicy(max_rounds=2)
    assert policy.should_continue(
        finish_reason,
        round_number,
        pending_tool_calls=pending_tools,
        cancelled=cancelled,
    ) is expected


def test_continuation_buffer_delays_bounded_prefix_then_emits_only_novel_text():
    buffer = ContinuationBuffer("alpha beta gamma", comparison_window=16)

    assert buffer.feed("beta ") == ""
    assert buffer.feed("gamma delta and more") == " delta and more"
    assert buffer.flush() == ""


def test_aggregate_usage_sums_rounds_and_safe_extension_fields():
    aggregate = aggregate_usage(
        [
            {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
                "prompt_tokens_details": {"cached_tokens": 3},
                "completion_tokens_details": {"reasoning_tokens": 2},
                "audio_tokens": 1,
            },
            {
                "prompt_tokens": 8,
                "completion_tokens": 5,
                "cached_tokens": 2,
                "reasoning_tokens": 1,
                "audio_tokens": 4,
                "unsafe": "ignore",
            },
        ]
    )

    assert aggregate == {
        "prompt_tokens": 18,
        "completion_tokens": 9,
        "total_tokens": 27,
        "cached_tokens": 5,
        "reasoning_tokens": 3,
        "audio_tokens": 5,
    }


def test_aggregate_usage_returns_none_without_numeric_usage():
    assert aggregate_usage([None, {}, {"request_id": "x"}]) is None


async def test_chat_service_persists_aggregate_usage_and_computes_cost_once(
    db_session, monkeypatch
):
    usage = {
        "prompt_tokens": 22,
        "completion_tokens": 5,
        "total_tokens": 27,
        "cached_tokens": 4,
        "reasoning_tokens": 2,
    }
    conversation = Conversation(
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        title="usage persistence",
    )
    db_session.add(conversation)
    await db_session.flush()
    message = Message(conversation_id=conversation.id, role="assistant", content="answer", metadata_={})
    db_session.add(message)
    monkeypatch.setattr(
        "app.core.pricing.get_settings",
        lambda: SimpleNamespace(
            MODEL_PRICING_JSON='{"gpt-test": {"prompt": 1, "completion": 2}}'
        ),
    )
    reset_pricing_cache()

    _apply_usage_accounting(message, "gpt-test", usage)
    await db_session.commit()
    message_id = message.id
    db_session.expire_all()
    persisted = await db_session.get(Message, message_id)

    assert persisted is not None
    assert persisted.prompt_tokens == 22
    assert persisted.completion_tokens == 5
    assert persisted.total_tokens == 27
    assert persisted.cost_usd == 0.000032
    assert persisted.metadata_["usage"] == usage
    reset_pricing_cache()


@pytest.mark.parametrize("terminal", ["error", "cancelled"])
async def test_chat_service_terminal_paths_persist_usage_and_cost_once(
    db_session, monkeypatch, terminal
):
    conversation = Conversation(
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        title=f"usage {terminal}",
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
    usage = {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}
    cost_calls = []
    monkeypatch.setattr(
        "app.core.pricing.compute_cost",
        lambda model_name, payload: cost_calls.append((model_name, payload)) or 0.25,
    )

    service = ChatService()
    if terminal == "error":
        await service._finalize_error(
            db_session,
            message,
            "failed",
            finish_reason="provider_error",
            code="provider_error",
            usage=usage,
            model_name="gpt-test",
        )
    else:
        await service._finalize_interrupted(
            db_session,
            message,
            finish_reason="cancelled",
            usage=usage,
            model_name="gpt-test",
        )

    assert cost_calls == [("gpt-test", usage)]
    assert message.prompt_tokens == 9
    assert message.completion_tokens == 3
    assert message.total_tokens == 12
    assert message.cost_usd == 0.25
    assert message.metadata_["usage"] == usage


async def test_chat_service_checkpoint_is_durable_and_idempotent(db_session):
    user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    conversation = Conversation(user_id=user_id, title="checkpoint")
    db_session.add(conversation)
    await db_session.flush()
    message = Message(
        conversation_id=conversation.id, role="assistant", content="partial", metadata_={}
    )
    db_session.add(message)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conversation.id,
        message_id=message.id,
        user_id=user_id,
        runtime="native",
        flow_name="single_agent",
        status="running",
    )
    db_session.add(run)
    await db_session.commit()
    checkpoint = {"round": 1, "max_rounds": 2, "status": "continuing"}

    await _persist_continuation_checkpoint(
        TestSessionLocal, message, run.id, checkpoint
    )
    await _persist_continuation_checkpoint(
        TestSessionLocal, message, run.id, checkpoint
    )
    message_id, run_id = message.id, run.id
    db_session.expire_all()

    persisted_message = await db_session.get(Message, message_id)
    persisted_run = await db_session.get(AgentRun, run_id)
    assert persisted_message.metadata_["continuation"] == checkpoint
    assert persisted_run.output["continuation"] == checkpoint
    assert persisted_run.current_step == "continuation:1"
    assert persisted_run.resume_token == "continuation:1"


async def test_independent_checkpoint_rollback_keeps_request_objects_usable(
    db_session,
):
    """A failed checkpoint transaction must expire only its private session."""

    class CancelFirstCommitSession(AsyncSession):
        fail_next_commit = True
        commit_calls = 0
        rollback_calls = 0

        async def commit(self):
            type(self).commit_calls += 1
            assert self.in_transaction()
            await self.flush()
            if type(self).fail_next_commit:
                type(self).fail_next_commit = False
                raise asyncio.CancelledError()
            await super().commit()

        async def rollback(self):
            type(self).rollback_calls += 1
            await super().rollback()

    checkpoint_sessions = async_sessionmaker(
        bind=db_session.bind,
        class_=CancelFirstCommitSession,
        expire_on_commit=False,
        autoflush=False,
    )
    user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    conversation = Conversation(user_id=user_id, title="isolated checkpoint")
    db_session.add(conversation)
    await db_session.flush()
    message = Message(
        conversation_id=conversation.id,
        role="assistant",
        content="partial answer",
        metadata_={
            "status": "pending",
            "continuation": {"round": 1, "max_rounds": 2, "status": "continuing"},
        },
        model_name="gpt-test",
    )
    db_session.add(message)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conversation.id,
        message_id=message.id,
        user_id=user_id,
        runtime="crewai",
        flow_name="deep_research",
        status="running",
        output={"seed": True},
    )
    db_session.add(run)
    await db_session.commit()

    continuing = {"round": 1, "max_rounds": 2, "status": "continuing"}
    with pytest.raises(asyncio.CancelledError):
        await _persist_continuation_checkpoint(
            checkpoint_sessions, message, run.id, continuing
        )

    assert CancelFirstCommitSession.rollback_calls == 1
    # These are request-session ORM objects. Access must remain synchronous;
    # an accidental rollback of db_session raises MissingGreenlet here.
    assert message.content == "partial answer"
    assert message.metadata_["continuation"]["status"] == "continuing"
    assert run.output == {"seed": True}

    cancelled = {"round": 1, "max_rounds": 2, "status": "cancelled"}
    message.metadata_ = {**message.metadata_, "continuation": cancelled}
    await _persist_continuation_checkpoint(
        checkpoint_sessions, message, run.id, cancelled
    )
    usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    await ChatOrchestrator()._finalize_run(
        db_session,
        run,
        ev_done(message_id=message.id, finish_reason="cancelled", usage=usage),
        session_factory=checkpoint_sessions,
    )
    await ChatService()._finalize_interrupted(
        db_session,
        message,
        finish_reason="cancelled",
        usage=usage,
        model_name="gpt-test",
    )

    async with checkpoint_sessions() as verify:
        durable_message = await verify.get(Message, message.id)
        durable_run = await verify.get(AgentRun, run.id)
        assert durable_message.content == "partial answer"
        assert durable_message.metadata_["status"] == "cancelled"
        assert durable_message.metadata_["continuation"] == cancelled
        assert durable_message.total_tokens == 12
        assert durable_run.status == "cancelled"
        assert durable_run.output["seed"] is True
        assert durable_run.output["continuation"] == cancelled
        assert durable_run.output["finish_reason"] == "cancelled"
        assert durable_run.output["usage"] == usage


async def test_run_finalization_preserves_durable_continuation_checkpoint(db_session):
    user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    conversation = Conversation(user_id=user_id, title="final checkpoint")
    db_session.add(conversation)
    await db_session.flush()
    message = Message(conversation_id=conversation.id, role="assistant", content="done")
    db_session.add(message)
    await db_session.flush()
    checkpoint = {"round": 1, "max_rounds": 2, "status": "continuing"}
    run = AgentRun(
        conversation_id=conversation.id,
        message_id=message.id,
        user_id=user_id,
        status="running",
        output={"continuation": checkpoint},
    )
    db_session.add(run)
    await db_session.flush()

    await ChatOrchestrator()._finalize_run(
        db_session,
        run,
        ev_done(message_id=message.id, finish_reason="stop"),
    )

    assert run.output["continuation"] == checkpoint
    assert run.output["finish_reason"] == "stop"
