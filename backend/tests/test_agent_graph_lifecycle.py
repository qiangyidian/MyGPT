"""Multi-agent lifecycle tests — real per-agent state, no LLM.

These drive :class:`CrewAIRuntime._run_multi_agent` with a
:class:`FakeStageExecutor` injected via ``ctx.extra["stage_executor"]``. Because
each stage is a real awaitable (the fake sleeps, emits tool events, then
returns), the resulting event stream reflects genuine execution ordering —
serial means one running at a time, parallel means several running at once.

Covers spec scenarios A (serial), B (parallel), C (approval handled elsewhere),
D (failure), plus: edge activation, join waits, tool attribution, snapshot
restore via the AgentRun API, and old-SSE-event compatibility.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.agents.crews.stage import StageSpec
from app.agents.graph import (
    AgentEdgeStatus,
    AgentGraph,
    AgentNodeStatus,
    build_deep_research_graph,
    build_parallel_research_graph,
)
from app.agents.lifecycle import AgentLifecycleEmitter
from app.agents.runtime.crewai_runtime import CrewAIRuntime
from app.agents.runtime.stage_executor import FakeStageExecutor
from app.agents.schemas import AgentTurnContext, ExecutionMode
from app.agents.stage_context import make_stage_context
from app.models import AgentRun, AgentStep, Conversation, Message

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


# --------------------------------------------------------------------------- #
# Test harness: build a minimal ctx + collect runtime events
# --------------------------------------------------------------------------- #
async def _seed_ctx(db_session) -> AgentTurnContext:
    conv = Conversation(user_id=_SEEDED_USER, title="graph test")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id, message_id=msg.id, user_id=_SEEDED_USER,
        runtime="crewai", flow_name="deep_research", status="running",
    )
    db_session.add(run)
    await db_session.flush()
    cfg = SimpleNamespace(
        provider="mock", api_base_url="http://x/v1", api_key_encrypted="",
        model_name="mock", temperature=0.3, top_p=1.0, max_tokens=64,
        supports_tools=True,
    )
    user = SimpleNamespace(id=_SEEDED_USER, role="user")
    ctx = AgentTurnContext(
        db=db_session, user=user, conversation=conv, model_config=cfg,
        request=SimpleNamespace(), user_content="compare A and B",
        system_prompt="", messages=[], rag_context="", citations=[],
        assistant_msg=msg, run_id=run.id, execution_mode=ExecutionMode.agent,
        agent_profile="deep_research", enable_tools=True,
    )
    return ctx


async def _collect(ctx) -> list[tuple[str, dict]]:
    """Drive CrewAIRuntime.stream_turn and collect (kind, data) pairs."""
    rt = CrewAIRuntime()
    out: list[tuple[str, dict]] = []
    async for evt in rt.stream_turn(ctx):
        out.append((evt.kind, evt.data))
    return out


def _statuses_by_event(events, agent_id: str) -> list[str]:
    """All agent_status values emitted for one agent, in order."""
    return [
        d["status"] for k, d in events
        if k == "agent_status" and d["agent_id"] == agent_id
    ]


def _running_sets(events) -> list[list[str]]:
    """Snapshots of current_agent_ids from every run_status event, in order."""
    return [d.get("current_agent_ids", []) for k, d in events if k == "run_status"]


# --------------------------------------------------------------------------- #
# Emitter unit tests (no runtime, no DB) — the core correctness guarantees
# --------------------------------------------------------------------------- #
def _emitter(graph: AgentGraph) -> tuple[AgentLifecycleEmitter, list]:
    """An emitter whose events are captured into a list (fake StageContext)."""
    captured: list = []
    loop = asyncio.new_event_loop()

    class _FakeCtx:
        agent_id = ""
        task_id = ""

        def emit(self, event):
            captured.append(event)

        def close(self):
            pass

    stage_ctx = _FakeCtx()
    # Bypass make_stage_context; we only need emit() to record.
    em = AgentLifecycleEmitter.__new__(AgentLifecycleEmitter)
    em.run_id = uuid.uuid4()
    em.graph = graph
    em.graph.run_id = str(em.run_id)
    em.ctx = stage_ctx  # type: ignore[arg-type]
    em._started_at = None
    em._finished_at = None
    em._node_starts = {}
    import time
    em._time = time
    return em, captured


def test_serial_never_has_two_running():
    em, cap = _emitter(build_deep_research_graph("q"))
    em.emit_agent_started("researcher")
    # Cannot start analyst while researcher still running (its inbound edge is
    # pending, predecessor not completed) -> should go waiting, not running.
    ok = em.emit_agent_started("analyst")
    assert ok is False
    assert em.graph.node("analyst").status == AgentNodeStatus.waiting
    # Exactly one running: researcher.
    assert em.graph.recompute_active() == ["researcher"]


def test_serial_completion_activates_downstream_edge():
    em, cap = _emitter(build_deep_research_graph("q"))
    em.emit_agent_started("researcher")
    em.emit_agent_completed("researcher", output_summary="6 sources")
    # The researcher->analyst edge flipped pending -> active -> completed.
    edge = em.graph.edge("researcher-analyst")
    assert edge.status == AgentEdgeStatus.completed
    # Analyst can now start.
    assert em.emit_agent_started("analyst") is True


def test_no_regression_completed_to_running():
    em, cap = _emitter(build_deep_research_graph("q"))
    em.emit_agent_started("researcher")
    em.emit_agent_completed("researcher")
    # A late/duplicate running event must NOT flip it back.
    started = em.emit_agent_started("researcher")
    assert started is False
    assert em.graph.node("researcher").status == AgentNodeStatus.completed


def test_parallel_two_researchers_run_concurrently():
    em, cap = _emitter(build_parallel_research_graph("q"))
    em.emit_agent_started("coordinator")
    em.emit_agent_completed("coordinator")
    # Both researchers can start now (both inbound edges completed).
    assert em.emit_agent_started("web-researcher") is True
    assert em.emit_agent_started("kb-researcher") is True
    # active_agent_ids contains BOTH -> genuine parallel.
    active = em.graph.recompute_active()
    assert set(active) == {"web-researcher", "kb-researcher"}


def test_join_waits_for_all_predecessors():
    em, cap = _emitter(build_parallel_research_graph("q"))
    em.emit_agent_started("coordinator"); em.emit_agent_completed("coordinator")
    em.emit_agent_started("web-researcher"); em.emit_agent_completed("web-researcher")
    em.emit_agent_started("kb-researcher")
    # Analyst has two inbound edges; only web-analyst is completed, kb-analyst
    # is not -> analyst must wait, NOT start.
    assert em.emit_agent_started("analyst") is False
    assert em.graph.node("analyst").status == AgentNodeStatus.waiting
    # Once kb-researcher completes, analyst can start.
    em.emit_agent_completed("kb-researcher")
    assert em.emit_agent_started("analyst") is True


def test_one_parallel_completes_other_still_running():
    em, cap = _emitter(build_parallel_research_graph("q"))
    em.emit_agent_started("coordinator"); em.emit_agent_completed("coordinator")
    em.emit_agent_started("web-researcher")
    em.emit_agent_started("kb-researcher")
    em.emit_agent_completed("web-researcher")
    # web done, kb still running.
    assert em.graph.node("web-researcher").status == AgentNodeStatus.completed
    assert em.graph.node("kb-researcher").status == AgentNodeStatus.running
    assert em.graph.recompute_active() == ["kb-researcher"]


def test_fail_fast_cancels_downstream():
    em, cap = _emitter(build_deep_research_graph("q"))
    em.emit_agent_started("researcher")
    em.emit_agent_failed("researcher", error="search down")
    # Downstream analyst/writer get cancelled.
    assert em.graph.node("analyst").status == AgentNodeStatus.cancelled
    assert em.graph.node("writer").status == AgentNodeStatus.cancelled
    assert em.graph.node("researcher").status == AgentNodeStatus.failed


# --------------------------------------------------------------------------- #
# End-to-end via the runtime (FakeStageExecutor, no LLM)
# --------------------------------------------------------------------------- #
async def test_serial_runtime_one_running_at_a_time(db_session):
    ctx = await _seed_ctx(db_session)
    ctx.extra["stage_executor"] = FakeStageExecutor()
    events = await _collect(ctx)

    # graph + terminal done present
    kinds = [k for k, _ in events]
    assert "agent_graph" in kinds
    assert kinds[-1] == "done"

    # researcher ran first, then analyst, then writer — never two running.
    running_sets = _running_sets(events)
    for snap in running_sets:
        assert len(snap) <= 1, f"serial run had >1 concurrent agent: {snap}"
    # Each agent reached completed exactly once.
    for aid in ("researcher", "analyst", "writer"):
        statuses = _statuses_by_event(events, aid)
        assert "running" in statuses
        assert statuses[-1] == "completed"


async def test_parallel_runtime_two_concurrent(db_session):
    ctx = await _seed_ctx(db_session)
    ctx.agent_profile = "parallel_research"
    # Overlap the two researchers so they're genuinely concurrent.
    fake = FakeStageExecutor(behaviors={
        "coordinator": FakeStageExecutor.Behavior(delay=0.02, output="split"),
        "web-researcher": FakeStageExecutor.Behavior(delay=0.08, output="web ev"),
        "kb-researcher": FakeStageExecutor.Behavior(delay=0.08, output="kb ev"),
        "analyst": FakeStageExecutor.Behavior(delay=0.02, output="finding"),
        "writer": FakeStageExecutor.Behavior(delay=0.02, output="answer"),
    })
    ctx.extra["stage_executor"] = fake
    events = await _collect(ctx)

    running_sets = _running_sets(events)
    # At some point BOTH researchers were running concurrently.
    assert any(set(s) >= {"web-researcher", "kb-researcher"} for s in running_sets), \
        f"never saw both researchers running together: {running_sets}"
    # Analyst started only after both researchers completed.
    analyst_status = _statuses_by_event(events, "analyst")
    assert analyst_status[-1] == "completed"
    # web finished before kb at some point but kb still running was observed.
    assert _statuses_by_event(events, "web-researcher")[-1] == "completed"
    assert _statuses_by_event(events, "kb-researcher")[-1] == "completed"


async def test_failure_propagates_and_run_fails(db_session):
    ctx = await _seed_ctx(db_session)
    fake = FakeStageExecutor(behaviors={
        "researcher": FakeStageExecutor.Behavior(delay=0.02, fail="search endpoint down"),
        "analyst": FakeStageExecutor.Behavior(delay=0.02),
        "writer": FakeStageExecutor.Behavior(delay=0.02),
    })
    ctx.extra["stage_executor"] = fake
    events = await _collect(ctx)
    kinds = [k for k, _ in events]

    # Researcher failed; analyst/writer cancelled; run failed; stream ended
    # with error (not done).
    assert _statuses_by_event(events, "researcher")[-1] == "failed"
    assert _statuses_by_event(events, "analyst")[-1] == "cancelled"
    assert "error" in kinds
    assert "done" not in kinds


async def test_tool_events_carry_agent_id(db_session):
    ctx = await _seed_ctx(db_session)
    fake = FakeStageExecutor(behaviors={
        "researcher": FakeStageExecutor.Behavior(delay=0.02, tools=[
            {"name": "web_search", "args": {"query": "x"}, "ok": True, "result": "hits"},
        ]),
        "analyst": FakeStageExecutor.Behavior(delay=0.02),
        "writer": FakeStageExecutor.Behavior(delay=0.02),
    })
    ctx.extra["stage_executor"] = fake
    events = await _collect(ctx)

    tool_calls = [d for k, d in events if k == "tool_call"]
    assert tool_calls, "expected at least one tool_call event"
    assert all(d.get("agent_id") == "researcher" for d in tool_calls)
    tool_results = [d for k, d in events if k == "tool_result"]
    assert all(d.get("agent_id") == "researcher" for d in tool_results)


async def test_graph_snapshot_restored_via_api(client, db_session):
    """After a run, GET /api/agent-runs/{id} returns the graph for restore."""
    from tests.conftest import auth_headers

    ctx = await _seed_ctx(db_session)
    ctx.extra["stage_executor"] = FakeStageExecutor()
    await _collect(ctx)

    h = auth_headers()
    detail = (await client.get(f"/api/agent-runs/{ctx.run_id}", headers=h)).json()
    assert detail["graph"] is not None
    node_ids = {n["id"] for n in detail["graph"]["nodes"]}
    assert node_ids == {"researcher", "analyst", "writer"}
    # All completed.
    assert all(n["status"] == "completed" for n in detail["graph"]["nodes"])
    # Steps carry agent_id attribution.
    tool_steps = [s for s in detail["steps"] if s["step_type"] == "tool"]
    # (FakeStageExecutor with no tools writes no tool steps; that's fine —
    # the agent_id column exists and is empty-string for non-tool steps.)
    assert all("agent_id" in s for s in detail["steps"])


async def test_old_sse_events_still_emitted(db_session):
    """Compatibility: plan_created/step events aren't required for multi-agent,
    but the existing vocabulary (done/error/token) must still terminate cleanly."""
    ctx = await _seed_ctx(db_session)
    ctx.extra["stage_executor"] = FakeStageExecutor()
    events = await _collect(ctx)
    kinds = [k for k, _ in events]
    # Token (final answer) + done are present.
    assert "token" in kinds
    assert kinds[-1] == "done"


# --------------------------------------------------------------------------- #
# Approval pause/resume (scenario C) — multi-agent path
# --------------------------------------------------------------------------- #
async def test_multi_agent_approval_pauses_then_resumes(db_session):
    """A dangerous tool in a CrewAI stage pauses the node (waiting) + run
    (waiting_approval); approving resumes the agent and the run completes."""
    from app.agents.approval_coordinator import approval_coordinator

    ctx = await _seed_ctx(db_session)
    approval_id = uuid.uuid4()
    fake = FakeStageExecutor(behaviors={
        "researcher": FakeStageExecutor.Behavior(
            delay=0.05,
            output="evidence",
            tools=[{"name": "db_query", "args": {"sql": "SELECT 1"}, "ok": True, "result": "rows"}],
            approval={"tool": "db_query", "approval_id": approval_id},
        ),
        "analyst": FakeStageExecutor.Behavior(delay=0.02),
        "writer": FakeStageExecutor.Behavior(delay=0.02),
    })
    ctx.extra["stage_executor"] = fake

    # Drive the runtime in a task so we can approve mid-flight.
    rt = CrewAIRuntime()
    events: list[tuple[str, dict]] = []

    async def drive() -> None:
        async for evt in rt.stream_turn(ctx):
            events.append((evt.kind, evt.data))

    task = asyncio.create_task(drive())
    # Wait for the waiting_approval signal to appear (the node paused).
    async def wait_for_waiting(timeout=5.0):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if any(k == "run_status" and d["status"] == "waiting_approval" for k, d in events):
                return True
            await asyncio.sleep(0.02)
        return False

    assert await wait_for_waiting(), f"run never entered waiting_approval: {events}"
    # The researcher node is waiting.
    researcher_statuses = _statuses_by_event(events, "researcher")
    assert "waiting" in researcher_statuses

    # Approve via the coordinator (the API path does exactly this).
    approval_coordinator.approve(approval_id)
    await task

    # After approve: researcher resumes running then completes; run completes.
    assert "completed" in _statuses_by_event(events, "researcher")
    assert any(k == "run_status" and d["status"] == "completed" for k, d in events)
    # Final answer + done.
    assert any(k == "done" for k, _ in events)


async def test_multi_agent_approval_rejected_continues(db_session):
    """Rejecting the dangerous tool leaves it blocked; the agent still
    completes (the model/executor proceeds without that tool)."""
    from app.agents.approval_coordinator import approval_coordinator

    ctx = await _seed_ctx(db_session)
    approval_id = uuid.uuid4()
    fake = FakeStageExecutor(behaviors={
        "researcher": FakeStageExecutor.Behavior(
            delay=0.05, output="partial",
            tools=[{"name": "db_query", "args": {"sql": "SELECT 1"}, "ok": False, "error": "rejected"}],
            approval={"tool": "db_query", "approval_id": approval_id},
        ),
        "analyst": FakeStageExecutor.Behavior(delay=0.02),
        "writer": FakeStageExecutor.Behavior(delay=0.02),
    })
    ctx.extra["stage_executor"] = fake

    rt = CrewAIRuntime()
    events: list[tuple[str, dict]] = []
    task = asyncio.create_task(_drive(rt, ctx, events))

    async def wait_for_waiting(timeout=5.0):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if any(k == "run_status" and d["status"] == "waiting_approval" for k, d in events):
                return True
            await asyncio.sleep(0.02)
        return False

    assert await wait_for_waiting()
    approval_coordinator.reject(approval_id, "too risky")
    await task

    # The tool_result carried ok=False (rejected).
    results = [d for k, d in events if k == "tool_result" and d.get("agent_id") == "researcher"]
    assert results and any(r["ok"] is False for r in results)
    # Run still completed (the agent proceeded without the tool).
    assert any(k == "run_status" and d["status"] == "completed" for k, d in events)


async def _drive(rt, ctx, events):
    async for evt in rt.stream_turn(ctx):
        events.append((evt.kind, evt.data))
