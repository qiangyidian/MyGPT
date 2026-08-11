"""Context compaction: token-budget trigger + summary/tail rebuild (Codex pattern)."""
from __future__ import annotations

from app.agents.context_compaction import (
    compact_messages,
    estimate_tokens,
    should_compact,
)


def test_estimate_tokens_positive_and_cjk_aware():
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello world") > 0
    # CJK text should register ~1 token per char (heuristic branch at minimum).
    assert estimate_tokens("你好世界") >= 4


def test_should_compact_on_body_growth_not_total():
    # Large static prefix but small body -> do NOT compact.
    assert should_compact(
        total_tokens=9000, prefill_baseline_tokens=8500,
        auto_compact_limit=2000, hard_window_tokens=16000,
    ) is False
    # Body grew past the limit -> compact.
    assert should_compact(
        total_tokens=12000, prefill_baseline_tokens=8500,
        auto_compact_limit=2000, hard_window_tokens=16000,
    ) is True
    # Hard window backstop.
    assert should_compact(
        total_tokens=16000, prefill_baseline_tokens=0,
        auto_compact_limit=10_000_000, hard_window_tokens=16000,
    ) is True


def test_compact_preserves_system_summarizes_older_keeps_tail():
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "old1 " + ("a" * 400)},
        {"role": "assistant", "content": "oldans1 " + ("b" * 400)},
        {"role": "user", "content": "recent1"},
        {"role": "assistant", "content": "recentans1"},
    ]

    def summarize(older):
        return "SUMMARY OF " + str(len(older))

    new_msgs, summary = compact_messages(
        messages, summarize_fn=summarize, keep_recent_tokens=20
    )
    # System preserved first.
    assert new_msgs[0] == {"role": "system", "content": "SYS"}
    # Summary inserted.
    assert any("SUMMARY OF 2" in m.get("content", "") for m in new_msgs)
    # Tail kept verbatim.
    assert {"role": "user", "content": "recent1"} in new_msgs
    assert {"role": "assistant", "content": "recentans1"} in new_msgs
    # Older messages dropped from verbatim tail.
    assert all("old1" not in m.get("content", "") for m in new_msgs)
    assert summary == "SUMMARY OF 2"


def test_compact_nothing_to_compact_returns_unchanged():
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "only one msg"},
    ]
    new_msgs, summary = compact_messages(messages, summarize_fn=lambda _: "X", keep_recent_tokens=10000)
    assert new_msgs == messages
    assert summary == ""


def test_compact_never_orphans_tool_result_or_tool_call():
    """Tool-pair retention is bidirectional: never emit a tool result whose
    caller was dropped, AND never emit an assistant tool_call whose result was
    dropped. Both produce invalid provider transcripts."""
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "old question " + "a" * 2000},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "s", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_a", "name": "s", "content": "ra " + "b" * 2000},
        {"role": "assistant", "content": "old answer " + "c" * 2000},
        {"role": "user", "content": "new question"},
    ]
    new_msgs, _ = compact_messages(
        messages, summarize_fn=lambda older: "OLD", keep_recent_tokens=30
    )
    # Invariant (closed transcript): every tool_call id that appears must also
    # appear as a tool result, and vice versa. No orphans in either direction.
    call_ids = {
        tc["id"] for m in new_msgs if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or []) if isinstance(tc, dict)
    }
    result_ids = {
        m["tool_call_id"] for m in new_msgs if m.get("role") == "tool"
    }
    assert result_ids == call_ids, (
        f"tool pair split: calls={call_ids} results={result_ids}"
    )
