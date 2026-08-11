"""Task 7: the ONE ContextManager — budget partitioning, tool-pair-aware
mid-run compaction, protected-fragment preservation, and output spill to
opaque artifact handles.

Pure-core / offline: no DB, no live LLM, no live Qdrant. The summarizer and
spill writer are injected, mirroring ``context_compaction.compact_messages``'s
``summarize_fn`` pattern.
"""
from __future__ import annotations

import pytest

from app.agents.context_compaction import estimate_tokens
from app.agents.context_manager import (
    ArtifactHandle,
    BudgetPartition,
    ContextManager,
)
from app.agents.token_budget import TokenBudget
from app.model_capabilities import ModelCapabilities


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _assistant_with_tool_call(call_id: str, *, name: str = "search", args: str = "{}"):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}
        ],
    }


def _tool_result(call_id: str, content: str, *, name: str = "search"):
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": content,
    }


def _has_complete_tool_pairs(msgs: list[dict]) -> bool:
    """True iff every ``tool`` role message has its matching assistant tool_call.

    A provider transcript is invalid if a ``tool`` result appears whose issuing
    ``assistant`` tool_call was dropped — that is exactly the orphan compaction
    must never produce.
    """
    call_ids: set[str] = set()
    for m in msgs:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                if tc.get("id"):
                    call_ids.add(tc["id"])
    result_ids: set[str] = set()
    for m in msgs:
        if m.get("role") == "tool":
            tid = m.get("tool_call_id")
            if tid:
                result_ids.add(tid)
    return result_ids.issubset(call_ids)


def _budget(input_tokens: int = 4000) -> TokenBudget:
    return TokenBudget(
        context_window=input_tokens + 1000,
        requested_output_tokens=1000,
        reserved_output_tokens=1000,
        tool_schema_tokens=0,
        safety_margin_tokens=500,
        input_tokens=input_tokens,
    )


# --------------------------------------------------------------------------- #
# Budget partitioning
# --------------------------------------------------------------------------- #
def test_partition_budget_against_token_budget():
    """ContextManager partitions the Task-1 TokenBudget into prompt slices."""
    mgr = ContextManager(summarize_fn=lambda older: "S")
    partition = mgr.partition_budget(_budget(input_tokens=8000))

    assert isinstance(partition, BudgetPartition)
    # The partitions are positive and sum is bounded by the input budget.
    assert partition.input_tokens == 8000
    assert partition.recent_keep_tokens > 0
    assert partition.protected_tokens >= 0
    assert partition.body_budget_tokens > 0
    # recent keep is a fraction of the input budget, never the whole thing.
    assert partition.recent_keep_tokens < partition.input_tokens


def test_partition_budget_is_model_aware():
    """A larger context window yields a larger recent-keep window."""
    mgr = ContextManager(summarize_fn=lambda older: "S")
    small = mgr.partition_budget(_budget(input_tokens=2000))
    large = mgr.partition_budget(_budget(input_tokens=20000))
    assert large.recent_keep_tokens > small.recent_keep_tokens


# --------------------------------------------------------------------------- #
# Tool-pair-aware compaction (the core Task-7 correctness rule)
# --------------------------------------------------------------------------- #
def test_compaction_keeps_tool_call_and_result_together():
    """A tool_call and its tool_result are never split by compaction."""
    history = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "please search"},
        _assistant_with_tool_call("tc_1"),
        _tool_result("tc_1", "result blob " + "x" * 4000),
        {"role": "assistant", "content": "old answer " + "y" * 4000},
        {"role": "user", "content": "follow up"},
        {"role": "assistant", "content": "recent answer"},
    ]
    mgr = ContextManager(summarize_fn=lambda older: "OLD SUMMARY")
    # Small budget forces compaction of the older prefix.
    compacted = mgr.compact(history, input_budget=300)

    assert _has_complete_tool_pairs(compacted)
    # Either BOTH the tool_call assistant and the tool result survived in the
    # verbatim tail, OR BOTH were summarized into the summary message. Asserting
    # the invariant (no orphan tool message) is the load-bearing check; we also
    # assert that if the tool result is present, its caller is too.
    tool_present = any(m.get("role") == "tool" for m in compacted)
    if tool_present:
        call_present = any(
            tc.get("id") == "tc_1"
            for m in compacted
            if m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or [])
        )
        assert call_present, "tool result kept but its tool_call was dropped"


def test_compact_never_emits_orphan_tool_message_on_large_history():
    """Multiple tool pairs across a long history never produce an orphan."""
    history: list[dict] = [{"role": "system", "content": "SYS"}]
    for i in range(8):
        history.append({"role": "user", "content": f"ask {i} " + "a" * 1500})
        history.append(_assistant_with_tool_call(f"tc_{i}"))
        history.append(_tool_result(f"tc_{i}", "r " + "b" * 1500))
        history.append({"role": "assistant", "content": f"ans {i} " + "c" * 1500})
    history.append({"role": "user", "content": "latest question"})

    mgr = ContextManager(summarize_fn=lambda older: f"SUMMARY of {len(older)}")
    compacted = mgr.compact(history, input_budget=500)
    assert _has_complete_tool_pairs(compacted)


def test_mid_run_compaction_triggers_when_body_exceeds_budget():
    """should_compact_midrun flags an in-flight transcript over budget."""
    mgr = ContextManager(summarize_fn=lambda older: "S")
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "x" * 2000},
    ]
    # Body well under budget -> no compaction.
    assert mgr.should_compact_midrun(messages, input_budget=10000) is False
    # Body over budget -> compact.
    big = [{"role": "system", "content": "SYS"}] + [
        {"role": "user", "content": "x" * 500} for _ in range(40)
    ]
    assert mgr.should_compact_midrun(big, input_budget=2000) is True


def test_mid_run_compaction_within_budget_is_noop():
    """compact() on a transcript that fits returns it unchanged (no summary)."""
    mgr = ContextManager(summarize_fn=lambda older: "SHOULD NOT BE CALLED")
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "short"},
        {"role": "assistant", "content": "reply"},
    ]
    out = mgr.compact(messages, input_budget=100_000)
    assert out == messages


# --------------------------------------------------------------------------- #
# Protected fragments
# --------------------------------------------------------------------------- #
def test_protected_fragments_survive_compaction():
    """Messages marked protected are never summarized away."""
    protected_system = {"role": "system", "content": "PROTECTED BASELINE " + "z" * 50}
    history = [
        protected_system,
        {"role": "user", "content": "old " + "a" * 3000},
        {"role": "assistant", "content": "old ans " + "b" * 3000},
        {"role": "user", "content": "recent q"},
    ]
    mgr = ContextManager(summarize_fn=lambda older: "OLD")
    compacted = mgr.compact(
        history, input_budget=200, protected_count=1
    )
    # The protected leading system message is preserved verbatim.
    assert compacted[0] == protected_system
    assert "PROTECTED BASELINE" in compacted[0]["content"]


# --------------------------------------------------------------------------- #
# Output spill → opaque artifact handle
# --------------------------------------------------------------------------- #
def test_spilled_tool_result_replaced_by_artifact_handle():
    """An oversized tool result is replaced by an opaque authorized handle."""
    written: dict[str, str] = {}

    def writer(key: str, content: str) -> str:
        written["key"] = key
        written["content"] = content
        written["returned"] = f"stored:{key}"  # opaque storage key, NOT a raw path
        return written["returned"]

    mgr = ContextManager(summarize_fn=lambda older: "S", spill_writer=writer)
    big_result = "GIGABYTE OF SCAN OUTPUT\n" + "x" * 20000

    in_context, handle = mgr.spill_tool_result(big_result, budget_tokens=100)

    assert isinstance(handle, ArtifactHandle)
    # The handle is opaque: it carries an id/key the model can reference but
    # cannot misuse as a filesystem path. It must NOT leak the raw path.
    assert handle.id.startswith("artifact:")
    assert "GIGABYTE" not in handle.id
    # The in-context preview is much smaller than the original.
    assert len(in_context) < len(big_result)
    # The handle references the storage key the writer returned.
    assert handle.storage_key == written["returned"]
    # The writer was invoked with the configured key + the full content.
    assert written["key"] == "tool_result"
    assert written["content"] == big_result


def test_spill_no_op_when_under_budget():
    """A small tool result is returned verbatim with no handle."""
    mgr = ContextManager(summarize_fn=lambda older: "S")
    in_context, handle = mgr.spill_tool_result("small", budget_tokens=1000)
    assert in_context == "small"
    assert handle is None


def test_spill_writer_failure_returns_original():
    """A spill-writer failure is best-effort: original retained, no handle."""

    def boom(key: str, content: str) -> str:
        raise OSError("disk full")

    mgr = ContextManager(summarize_fn=lambda older: "S", spill_writer=boom)
    big = "x" * 20000
    in_context, handle = mgr.spill_tool_result(big, budget_tokens=100)
    assert handle is None
    assert in_context == big  # original retained, never blocked


# --------------------------------------------------------------------------- #
# Complete effective system prompt (pure, no process-local world state)
# --------------------------------------------------------------------------- #
def test_assemble_system_prompt_is_pure_and_complete():
    """assemble_system_prompt is a pure function of persisted fragments — two
    calls with the same inputs produce the same prompt (no mutable cache)."""
    mgr = ContextManager(summarize_fn=lambda older: "S")
    out1 = mgr.assemble_system_prompt(
        base="You are helpful.",
        rag_context="CTX",
        summary="EARLIER SUMMARY",
        goal="ship Task 7",
        memories=["prefers concise answers", "uses Python"],
        intent_block=None,
        behavior_blocks=["MODE: expert"],
    )
    out2 = mgr.assemble_system_prompt(
        base="You are helpful.",
        rag_context="CTX",
        summary="EARLIER SUMMARY",
        goal="ship Task 7",
        memories=["prefers concise answers", "uses Python"],
        intent_block=None,
        behavior_blocks=["MODE: expert"],
    )
    assert out1 == out2  # pure
    # Complete: every persisted fragment is present in the effective prompt.
    assert "You are helpful." in out1
    assert "CTX" in out1
    assert "EARLIER SUMMARY" in out1
    assert "ship Task 7" in out1
    assert "prefers concise answers" in out1
    assert "uses Python" in out1
    assert "MODE: expert" in out1


def test_assemble_system_prompt_includes_active_memories_only_when_provided():
    """When the memory list is empty (no opt-in), nothing memory-related is
    injected — the prompt stays complete but uncluttered."""
    mgr = ContextManager(summarize_fn=lambda older: "S")
    without = mgr.assemble_system_prompt(
        base="B", rag_context="", summary="", goal="", memories=[], intent_block=None,
        behavior_blocks=[],
    )
    with_mem = mgr.assemble_system_prompt(
        base="B", rag_context="", summary="", goal="",
        memories=["only active memory"],
        intent_block=None, behavior_blocks=[],
    )
    assert "only active memory" not in without
    assert "only active memory" in with_mem


# --------------------------------------------------------------------------- #
# Model-switch downshift triggers mid-run compaction
# --------------------------------------------------------------------------- #
def test_downshift_compaction_directive():
    """On a context-window downshift, ContextManager advises a recompact whose
    target budget is the NEW (smaller) window and actually shrinks the transcript."""
    mgr = ContextManager(summarize_fn=lambda older: f"SUMMARY of {len(older)}")
    active_messages = [{"role": "system", "content": "SYS"}]
    # Several older turns that can be summarized into one summary block.
    # Large enough that the total clearly exceeds the new 8k window regardless
    # of tokenizer.
    for i in range(10):
        active_messages.append({"role": "user", "content": f"ask {i} " + "a" * 3000})
        active_messages.append({"role": "assistant", "content": f"ans {i} " + "b" * 3000})
    active_messages.append({"role": "user", "content": "latest question"})

    directive = mgr.downshift_compaction(
        previous_window_tokens=200_000,
        current_window_tokens=8_000,
        active_messages=active_messages,
    )
    assert directive.must_recompact is True
    # The new effective budget respects the smaller window.
    assert directive.input_budget <= 8_000
    # And compaction actually shrinks the transcript (summary replaced the
    # older prefix; the verbatim recent tail is much smaller than the input).
    assert len(directive.compacted_messages) < len(active_messages)
    assert any("SUMMARY of" in (m.get("content") or "") for m in directive.compacted_messages)


def test_downshift_no_recompact_when_window_grows():
    """An upshift (larger window) never requires recompaction."""
    mgr = ContextManager(summarize_fn=lambda older: "S")
    directive = mgr.downshift_compaction(
        previous_window_tokens=8_000,
        current_window_tokens=200_000,
        active_messages=[{"role": "system", "content": "SYS"}],
    )
    assert directive.must_recompact is False
