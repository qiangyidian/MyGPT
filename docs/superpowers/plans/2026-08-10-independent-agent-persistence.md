# Independent Agent Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent checkpoint or graph rollback from expiring ORM objects owned by the live chat request session.

**Architecture:** ChatService injects an independent `AsyncSessionLocal` factory into the turn context. Continuation, plan, and graph persistence open short-lived sessions and issue explicit ID-based SQL updates; their rollback affects only those sessions, while request-session locking remains limited to request-owned lifecycle and terminal writes.

**Tech Stack:** Python asyncio, SQLAlchemy AsyncSession/sessionmaker, SQLite integration tests, pytest.

---

### Task 1: Reproduce independent-session cancellation safety

**Files:**
- Modify: `backend/tests/test_continuation.py`
- Modify: `backend/tests/test_streaming_writer.py`

- [x] Add a real SQLite sessionmaker whose first independent checkpoint commit flushes then raises `CancelledError`.
- [x] Assert compensation uses a second session and request-session `Message`/`AgentRun` remain synchronously readable.
- [x] Assert cancelled checkpoint, partial content, usage, message terminal state, and run cancellation are durable.
- [x] Run the new tests and verify request-session checkpointing fails the independent-session contract.

### Task 2: Move continuation persistence off the request session

**Files:**
- Modify: `backend/app/services/chat_service.py`
- Modify: `backend/app/agents/runtime/native_runtime.py`
- Modify: `backend/app/agents/runtime/crewai_runtime.py`
- Modify: `backend/app/agents/streaming_writer.py`
- Modify: `backend/app/agents/stage_context.py`

- [x] Change checkpoint persistence to accept a session factory and use explicit `UPDATE` statements by message/run ID.
- [x] Inject `AsyncSessionLocal` from ChatService without acquiring the request DB lock.
- [x] Route native and Crew fallback checkpoint paths through the injected factory only.
- [x] Run focused tests and verify the cancellation compensator is green.

### Task 3: Isolate graph and best-effort Crew persistence

**Files:**
- Modify: `backend/app/agents/runtime/crewai_runtime.py`
- Modify: `backend/tests/test_streaming_writer.py`
- Modify: `backend/tests/test_agent_graph_lifecycle.py`

- [x] Persist plan and graph snapshots with short-lived independent sessions and explicit run-ID updates.
- [x] Add a real-session graph failure regression proving the request objects remain usable and failure does not become `MissingGreenlet`.
- [x] Update direct-runtime test contexts to inject their SQLite-bound sessionmaker.
- [x] Run graph, continuation, native, and Crew lifecycle suites.

### Task 4: Verify and commit

**Files:**
- Verify all modified production, test, and plan files.

- [x] Run focused RED/GREEN tests, expanded agent/chat suites, and full backend pytest.
- [x] Run compileall and diff checks.
- [x] Commit and report SHA with exact evidence.
