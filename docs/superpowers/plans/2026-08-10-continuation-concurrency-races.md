# Continuation Concurrency Races Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attribute cumulative CrewAI usage exactly once under parallel shared-LLM execution and eliminate stale continuation checkpoints across cancellation races.

**Architecture:** Put a per-LLM coordinator on `StageContext`, keyed by object identity and scoped to one run. A short per-LLM lock atomically claims `current_cumulative - last_claimed` only when a stage finishes or fails; model calls remain parallel and distinct models use distinct locks. Continuation runtimes re-check cancellation around awaited checkpoint persistence and synchronously replace `continuing` with `cancelled` before any terminal event or second provider dispatch.

**Tech Stack:** Python 3.12, asyncio, CrewAI stage runtime, SQLAlchemy async sessions, pytest.

---

### Task 1: Per-LLM usage coordinator

**Files:**
- Modify: `backend/app/agents/stage_context.py`
- Modify: `backend/app/agents/runtime/stage_executor.py`
- Test: `backend/tests/test_streaming_writer.py`

- [ ] Add a concurrent test where two agents share one cumulative fake LLM and actual final usage is `prompt_tokens=30`, `completion_tokens=5`; assert aggregate equals that final total, not overlapping deltas.
- [ ] Add concurrent failure/retry tests and a distinct-LLM timing test proving unrelated models are not globally serialized.
- [ ] Run the new tests and confirm overlapping snapshots overcount or failed usage is lost.
- [ ] Add a run-scoped identity-keyed coordinator with one async lock per LLM; lock only baseline/claim bookkeeping, never the `aexecute_task` call.
- [ ] Keep writer direct-provider usage outside CrewAI LLM summary accounting and re-run tests.

### Task 2: Cancellation checkpoint races

**Files:**
- Modify: `backend/app/agents/runtime/native_runtime.py`
- Modify: `backend/app/agents/streaming_writer.py`
- Test: `backend/tests/test_native_runtime_graph_events.py`
- Test: `backend/tests/test_streaming_writer.py`

- [ ] Add native and writer tests where cancellation is set during awaited `continuing` persistence; assert ordering ends with persisted `cancelled`, AgentRun no longer says `continuing`, and provider call count stays one.
- [ ] Add a native loop-top cancellation test after a successful continuing checkpoint and before provider dispatch.
- [ ] Run the tests and confirm stale `continuing` state or an unwanted second provider call.
- [ ] Re-check cancellation after checkpoint await and at loop top, persist `cancelled`, and stop/re-raise consistently before dispatch.
- [ ] Re-run all race tests and existing terminal checkpoint tests.

### Task 3: Verification and commit

**Files:**
- Verify all modified production, test, and plan files.

- [ ] Run focused continuation/concurrency/race suites.
- [ ] Run expanded CrewAI/agent/chat regressions.
- [ ] Run full backend pytest and isolate only the documented Tavily environment failure if present.
- [ ] Run compileall, working/staged diff checks, commit, and report SHA plus RED/GREEN evidence.
