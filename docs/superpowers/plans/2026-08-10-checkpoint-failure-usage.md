# Checkpoint Failure Usage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve completed provider-round usage exactly once when continuation checkpoint persistence aborts a Writer or Native turn.

**Architecture:** Before propagating a checkpoint persistence exception, move all completed but otherwise unreturned Writer usage into the idempotent `StageContext.usage_records` sink. Verify Native already updates `ctx.extra["usage"]` before checkpoint persistence, and change it only if the regression test proves a gap.

**Tech Stack:** Python 3.12, asyncio, SQLAlchemy async sessions, pytest.

---

### Task 1: Writer checkpoint exceptions

**Files:**
- Modify: `backend/app/agents/streaming_writer.py`
- Test: `backend/tests/test_streaming_writer.py`

- [ ] Add parameterized tests for `CancelledError` and `RuntimeError` raised while persisting `continuing`, after provider usage `10/2` and partial text were received.
- [ ] Run the tests and confirm `usage_records` is empty while content/checkpoint behavior remains observable.
- [ ] Record completed `usage_rounds` before cancellation compensation or generic propagation, using stable keys so later error handling cannot double-count.
- [ ] Re-run the new tests and existing continuation failure tests.

### Task 2: Native parity

**Files:**
- Modify only if needed: `backend/app/agents/runtime/native_runtime.py`
- Test: `backend/tests/test_native_runtime_graph_events.py`

- [ ] Add checkpoint exception tests asserting `ctx.extra["usage"]` remains `10/2`, partial content survives, provider call count is one, and terminal behavior matches cancellation/error handling.
- [ ] Run the tests and modify Native only if the test proves usage or finalization is missing.

### Task 3: Verification and commit

**Files:**
- Verify all modified production, test, and plan files.

- [ ] Run focused continuation/writer/native/chat tests.
- [ ] Run expanded agent/CrewAI/chat regressions and full backend pytest.
- [ ] Run compileall and working/staged diff checks.
- [ ] Commit and report SHA with RED/GREEN evidence.
