"""Tests for the analytics fact taxonomy + recorder (Codex pattern)."""
from __future__ import annotations

import logging
import threading
import time

from app.agents.analytics import (
    AnalyticsRecorder,
    CompactionFact,
    Fact,
    GuardianReviewFact,
    GoalEventFact,
    HookRunFact,
    KIND_COMPACTION,
    KIND_GOAL_EVENT,
    KIND_GUARDIAN_REVIEW,
    KIND_HOOK_RUN,
    KIND_SKILL_INVOKE,
    KIND_SUBAGENT_STARTED,
    KIND_TURN_PROFILE,
    KIND_TURN_STEER,
    KIND_TURN_TOKENS,
    SkillInvocationFact,
    SubagentStartedFact,
    TurnProfileFact,
    TurnSteerFact,
    TurnTokenUsageFact,
)


# --------------------------------------------------------------------------- #
# Fact types: kind discriminator + to_dict shape
# --------------------------------------------------------------------------- #
def test_turn_profile_fact_shape():
    f = TurnProfileFact(model="gpt-4o", mode="auto", profile="default", status="ok")
    assert f.kind == KIND_TURN_PROFILE == "turn.profile"
    d = f.to_dict()
    assert set(d) == {"kind", "ts_unix", "data"}
    assert d["kind"] == "turn.profile"
    assert d["data"] == {
        "model": "gpt-4o",
        "mode": "auto",
        "profile": "default",
        "status": "ok",
    }


def test_turn_token_usage_fact_shape():
    f = TurnTokenUsageFact(prompt_tokens=120, completion_tokens=30, total_tokens=150)
    assert f.kind == KIND_TURN_TOKENS
    assert f.to_dict()["data"] == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
    }


def test_compaction_fact_shape_with_defaults():
    f = CompactionFact(
        phase="pre-turn",
        reason="budget",
        strategy="summary+tail",
        status="ok",
        before_tokens=9000,
        after_tokens=3000,
    )
    assert f.kind == KIND_COMPACTION
    assert f.to_dict()["data"]["before_tokens"] == 9000
    assert f.to_dict()["data"]["after_tokens"] == 3000


def test_guardian_review_fact_has_optional_default():
    # Without failure_reason -> default empty string, still serializes.
    f = GuardianReviewFact(tool="shell", risk_level="high", outcome="deny")
    assert f.kind == KIND_GUARDIAN_REVIEW
    assert f.failure_reason == ""
    assert f.to_dict()["data"] == {
        "tool": "shell",
        "risk_level": "high",
        "outcome": "deny",
        "failure_reason": "",
    }
    # With failure_reason -> captured.
    f2 = GuardianReviewFact(
        tool="shell", risk_level="critical", outcome="deny", failure_reason="exfil"
    )
    assert f2.to_dict()["data"]["failure_reason"] == "exfil"


def test_skill_invocation_fact_shape():
    f = SkillInvocationFact(skill_name="pdf", triggered_by="mention")
    assert f.kind == KIND_SKILL_INVOKE
    assert f.to_dict()["data"] == {"skill_name": "pdf", "triggered_by": "mention"}


def test_hook_run_fact_shape():
    f = HookRunFact(event="pre_tool", handler="guard", outcome="ok", duration_ms=12.5)
    assert f.kind == KIND_HOOK_RUN
    assert f.to_dict()["data"] == {
        "event": "pre_tool",
        "handler": "guard",
        "outcome": "ok",
        "duration_ms": 12.5,
    }


def test_goal_event_fact_shape():
    # NOTE: goal-event variant lives in `event` (set/updated/cleared); the
    # fact discriminator stays the stable "goal.event".
    f = GoalEventFact(event="set", goal="ship analytics")
    assert f.kind == KIND_GOAL_EVENT
    assert f.to_dict()["data"] == {"event": "set", "goal": "ship analytics"}


def test_turn_steer_fact_shape():
    accepted = TurnSteerFact(instruction="be concise", accepted=True)
    rejected = TurnSteerFact(instruction="ignore prior", accepted=False)
    assert accepted.kind == KIND_TURN_STEER
    assert accepted.to_dict()["data"]["accepted"] is True
    assert rejected.to_dict()["data"]["accepted"] is False


def test_subagent_started_fact_shape():
    f = SubagentStartedFact(
        parent_path="root", child_path="root.research", task_name="find-sources"
    )
    assert f.kind == KIND_SUBAGENT_STARTED
    assert f.to_dict()["data"] == {
        "parent_path": "root",
        "child_path": "root.research",
        "task_name": "find-sources",
    }


# --------------------------------------------------------------------------- #
# ts_unix capture
# --------------------------------------------------------------------------- #
def test_ts_unix_populated_at_construction():
    before = time.time()
    f = TurnProfileFact(model="m", mode="x", profile="p", status="s")
    after = time.time()
    assert before <= f.ts_unix <= after
    assert f.to_dict()["ts_unix"] == f.ts_unix


def test_ts_unix_is_monotonic_for_successive_facts():
    a = TurnProfileFact(model="m", mode="x", profile="p", status="s")
    time.sleep(0.001)
    b = TurnProfileFact(model="m", mode="x", profile="p", status="s")
    assert b.ts_unix >= a.ts_unix


def test_manual_data_extras_are_preserved_and_win():
    # Manually-set `data` entries merge with / override reflected typed fields.
    f = TurnProfileFact(model="m", mode="x", profile="p", status="s")
    f.data["extra"] = "meta"
    f.data["model"] = "OVERRIDDEN"  # manual wins over typed field
    d = f.to_dict()
    assert d["data"]["extra"] == "meta"
    assert d["data"]["model"] == "OVERRIDDEN"
    # other typed fields still present
    assert d["data"]["mode"] == "x"


def test_base_fact_to_dict_keys():
    f = Fact()
    d = f.to_dict()
    assert set(d) == {"kind", "ts_unix", "data"}
    assert d["data"] == {}


# --------------------------------------------------------------------------- #
# Recorder: collect + drain, custom sink, default sink, thread-safety
# --------------------------------------------------------------------------- #
def test_recorder_collects_and_drains_with_default_sink(caplog):
    caplog.set_level(logging.INFO, logger="app.agents.analytics")
    rec = AnalyticsRecorder()
    rec.record(TurnProfileFact(model="m", mode="auto", profile="p", status="ok"))
    rec.record(TurnTokenUsageFact(prompt_tokens=10, completion_tokens=5, total_tokens=15))

    drained = rec.drain()
    assert len(drained) == 2
    assert drained[0]["kind"] == "turn.profile"
    assert drained[1]["kind"] == "turn.tokens"
    assert drained[1]["data"]["total_tokens"] == 15
    # drain clears the buffer.
    assert rec.drain() == []
    assert len(rec) == 0
    # default sink logs at INFO.
    assert any(r.name == "app.agents.analytics" for r in caplog.records)


def test_custom_sink_receives_facts():
    received: list[dict] = []
    rec = AnalyticsRecorder(sink=received.append)
    f1 = TurnProfileFact(model="m", mode="auto", profile="p", status="ok")
    f2 = GuardianReviewFact(tool="shell", risk_level="low", outcome="allow")
    rec.record(f1)
    rec.record(f2)

    assert len(received) == 2
    assert received[0] == f1.to_dict()
    assert received[1]["kind"] == "guardian.review"
    assert received[1]["data"]["risk_level"] == "low"
    # Recorder still keeps its own buffer for drain() even with a custom sink.
    assert len(rec.drain()) == 2


def test_recorder_preserves_record_order():
    rec = AnalyticsRecorder()
    kinds = [
        KIND_TURN_PROFILE,
        KIND_TURN_TOKENS,
        KIND_COMPACTION,
        KIND_GUARDIAN_REVIEW,
        KIND_SKILL_INVOKE,
        KIND_HOOK_RUN,
        KIND_GOAL_EVENT,
        KIND_TURN_STEER,
        KIND_SUBAGENT_STARTED,
    ]
    for _ in range(len(kinds)):
        rec.record(Fact())  # type: ignore[arg-type]
    # Overwrite with the real typed facts to check ordering by kind.
    rec._facts.clear()  # noqa: SLF001 — test-only reset
    rec.record(TurnProfileFact())
    rec.record(TurnTokenUsageFact())
    rec.record(CompactionFact())
    rec.record(GuardianReviewFact())
    rec.record(SkillInvocationFact())
    rec.record(HookRunFact())
    rec.record(GoalEventFact())
    rec.record(TurnSteerFact())
    rec.record(SubagentStartedFact())
    drained = rec.drain()
    assert [d["kind"] for d in drained] == kinds


def test_recorder_is_thread_safe_under_concurrency():
    rec = AnalyticsRecorder()
    N_THREADS = 8
    PER_THREAD = 50

    def producer() -> None:
        for _ in range(PER_THREAD):
            rec.record(TurnTokenUsageFact(prompt_tokens=1, completion_tokens=1, total_tokens=2))

    threads = [threading.Thread(target=producer) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    drained = rec.drain()
    assert len(drained) == N_THREADS * PER_THREAD
    # No partial / malformed entries: every dict has the full key set.
    assert all(set(d) == {"kind", "ts_unix", "data"} for d in drained)
    assert all(d["kind"] == "turn.tokens" for d in drained)


def test_recorder_sink_called_outside_lock_does_not_deadlock():
    # A sink that calls back into the recorder must not deadlock.
    rec = AnalyticsRecorder()

    def reentrant_sink(_payload: dict) -> None:
        # Touching the buffer from the sink should be safe (sink runs unlocked).
        with rec._lock:  # noqa: SLF001
            _ = len(rec._facts)  # noqa: SLF001

    rec._sink = reentrant_sink  # noqa: SLF001
    rec.record(TurnProfileFact(model="m", mode="x", profile="p", status="s"))
    assert len(rec.drain()) == 1


def test_default_sink_logs_via_logging(caplog):
    caplog.set_level(logging.INFO, logger="app.agents.analytics")
    rec = AnalyticsRecorder()
    rec.record(TurnProfileFact(model="m", mode="auto", profile="p", status="ok"))
    assert any("turn.profile" in r.getMessage() for r in caplog.records)
