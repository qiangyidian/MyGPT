# CrewAI DB Serialization and Cancellation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent concurrent AsyncSession mutation/poisoning in CrewAI turns and prevent cancelled flow tasks from being reported as successful.

**Architecture:** Create one request-scoped async lock in ChatService and pass it through `AgentTurnContext`/`StageContext`; every checkpoint and Crew graph mutation acquires that lock only around database work. Persistence helpers own rollback on every `BaseException`, including cancellation, and callers avoid acquiring the same lock twice. The Crew drainer always awaits the flow task after queue closure and propagates its cancellation instead of inferring success from an empty exception slot.

**Tech Stack:** Python 3.12, asyncio, SQLAlchemy AsyncSession, CrewAI runtime, pytest.

---

### Task 1: Serialized transaction ownership

**Files:**
- Modify: `backend/app/services/chat_service.py`
- Modify: `backend/app/agents/runtime/crewai_runtime.py`
- Modify: `backend/app/agents/stage_context.py`
- Test: `backend/tests/test_continuation.py`
- Test: `backend/tests/test_streaming_writer.py`

- [x] Add an AsyncSession-like detector test that raises on overlapping commit/get entry while checkpoint and graph persistence run concurrently.
- [x] Add commit-cancellation poisoning tests proving checkpoint persistence rolls back before re-raising and a later terminal status/usage commit succeeds.
- [x] Run tests and confirm overlap or missing rollback.
- [x] Share one request-scoped async lock across checkpoint and graph helpers, with lock acquisition only at top-level mutation boundaries.
- [x] Catch `BaseException` in checkpoint/graph transaction owners, shield/complete rollback, and re-raise.

### Task 2: Crew cancellation result propagation

**Files:**
- Modify: `backend/app/agents/runtime/crewai_runtime.py`
- Test: `backend/tests/test_streaming_writer.py`

- [x] Add a full Crew runtime test where Writer checkpoint cancellation happens after one provider call and persists a cancelled checkpoint.
- [x] Assert partial content and usage survive, provider call count remains one, and no `done/stop` is emitted.
- [x] Run the test and confirm the already-finished cancelled flow task is misreported as successful.
- [x] Always await/inspect `run_task` after drain, retain `CancelledError`, and propagate it to ChatService interruption handling.
- [x] Re-run cancellation and lifecycle regressions.

### Task 3: Verification and commit

**Files:**
- Verify all modified production, test, and plan files.

- [x] Run focused transaction/cancellation/continuation tests.
- [x] Run expanded CrewAI/agent/chat regressions and full backend pytest.
- [x] Run compileall plus working/staged diff checks.
- [x] Commit and report SHA with exact RED/GREEN evidence.
