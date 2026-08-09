# Continuation Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make continuation accounting complete for real CrewAI calls and tools, and make every continuation terminal state durable.

**Architecture:** Capture CrewAI LLM counters as before/after snapshots around each stage and record numeric deltas even on exceptions. Carry validated tool metering as a separate structured field from tool output, then let both runtimes aggregate that field once. Reuse the existing checkpoint callback for both dispatch and terminal checkpoints so Message and AgentRun remain synchronized.

**Tech Stack:** Python 3.12, asyncio, CrewAI adapters, SQLAlchemy async sessions, pytest.

---

### Task 1: CrewAI cumulative LLM usage

**Files:**
- Modify: `backend/app/agents/runtime/stage_executor.py`
- Test: `backend/tests/test_streaming_writer.py`

- [ ] Add tests whose agents return raw strings while their fake LLM exposes cumulative sync/async `get_token_usage_summary()` snapshots, including repeated attempts and an exception.
- [ ] Run the new tests and confirm usage is absent or cumulative totals are double-counted.
- [ ] Add numeric snapshot normalization/delta helpers and record each stage attempt in `finally`.
- [ ] Re-run the new tests and the existing CrewAI usage tests.

### Task 2: Durable terminal continuation checkpoints

**Files:**
- Modify: `backend/app/agents/runtime/native_runtime.py`
- Modify: `backend/app/agents/streaming_writer.py`
- Test: `backend/tests/test_native_runtime_graph_events.py`
- Test: `backend/tests/test_streaming_writer.py`

- [ ] Add DB-backed tests proving `maxed`, `completed`, and `cancelled` replace stale `continuing` state in both Message and AgentRun before terminal events.
- [ ] Add a persistence-failure test proving the runtime emits/raises a consistent failure instead of reporting an unpersisted terminal state.
- [ ] Run the tests and confirm stale `continuing` state or missing persistence.
- [ ] Route terminal checkpoints through the same required callback/fallback as pre-dispatch checkpoints and re-run tests.

### Task 3: Tool usage end to end

**Files:**
- Modify: `backend/app/agents/schemas.py`
- Modify: `backend/app/agents/gateway/tool_gateway.py`
- Modify: `backend/app/agents/adapters/tool_adapter.py`
- Modify: `backend/app/agents/runtime/native_runtime.py`
- Modify: `backend/app/agents/stage_context.py`
- Test: `backend/tests/test_continuation.py`
- Test: `backend/tests/test_native_runtime_graph_events.py`

- [ ] Add gateway and native tests proving raw numeric usage is carried separately, excluded from rendered model content, emitted safely, and aggregated once.
- [ ] Run tests and confirm usage is lost on the real gateway/native path.
- [ ] Add a strict numeric usage sanitizer and structured `ToolExecution.usage`, then wire adapters/events/runtime aggregation.
- [ ] Re-run focused tests and verify invalid/sensitive result fields never enter metering.

### Task 4: Verification and commit

**Files:**
- Verify all modified production and test files.

- [ ] Run the prior focused continuation/CrewAI/agent/chat suites plus all new tests.
- [ ] Run full backend pytest; isolate only the documented Tavily environment default failure if present.
- [ ] Run `python -m compileall -q app`, `git diff --check`, and staged diff checks.
- [ ] Commit the reviewed change and report the SHA with RED/GREEN evidence.
