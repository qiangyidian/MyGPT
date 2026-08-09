from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agents.continuation import (
    ContinuationBuffer,
    ContinuationPolicy,
    aggregate_usage,
    merge_continuation,
)
from app.core.config import Settings
from app.core.pricing import reset_pricing_cache
from app.models import Conversation, Message
from app.services.chat_service import _apply_usage_accounting


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


def test_settings_configures_continuation_rounds_with_same_bounds():
    assert Settings(AUTO_CONTINUATION_MAX_ROUNDS=3).AUTO_CONTINUATION_MAX_ROUNDS == 3
    with pytest.raises(ValidationError):
        Settings(AUTO_CONTINUATION_MAX_ROUNDS=-1)
    with pytest.raises(ValidationError):
        Settings(AUTO_CONTINUATION_MAX_ROUNDS=9)


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
