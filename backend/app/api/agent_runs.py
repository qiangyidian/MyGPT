"""Agent-runs API (Phase 3): inspect a run and drive human-in-the-loop approvals.

  GET    /api/agent-runs                       list runs (optionally by conversation)
  GET    /api/agent-runs/{run_id}              run detail + steps + approvals
  GET    /api/agent-runs/{run_id}/events       cursor-replay SSE (Task 5)
  POST   /api/agent-runs/{run_id}/approve      approve a pending dangerous tool
  POST   /api/agent-runs/{run_id}/reject       reject a pending dangerous tool
  POST   /api/agent-runs/{run_id}/cancel       cancel a waiting run

Approve/reject also signal the in-process :class:`ApprovalCoordinator` so a
paused live stream resumes from the exact step. Cancel flips the run to
``cancelled`` and cancels any pending wait. Ownership is enforced (admin may
view/act on any run).

The ``/events`` endpoint (Task 5) is a read-only SSE subscription that replays
the durable event log from a ``Last-Event-ID`` cursor then tails new events.
It NEVER executes or cancels the run — a client disconnect closes only the
subscription, the run keeps going.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.approval_bus import approval_bus
from app.agents.events import EventStore
from app.agents.run_controls import get as get_run_control
from app.agents.workflow import controls as durable_controls
from app.services import audit_service
from app.core.config import get_settings
from app.core.deps import get_current_user
from app.core.rate_limit import rate_limit_user
from app.db import get_db
from app.models import AgentRun, AgentStep, ToolApproval, ToolCall, User
from app.schemas import (
    AgentRunOut,
    AgentStepOut,
    ApproveRequest,
    ActionResult,
    PlanUpdateRequest,
    RejectRequest,
    RunInstructionRequest,
    ToolApprovalOut,
    ToolCallAuditOut,
)

router = APIRouter(prefix="/api/agent-runs", tags=["agent-runs"])

NOT_FOUND = status.HTTP_404_NOT_FOUND
FORBID = status.HTTP_403_FORBIDDEN


async def _load_run(db: AsyncSession, run_id: uuid.UUID) -> AgentRun:
    run = await db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(NOT_FOUND, "Run not found")
    return run


async def _assert_owned(run: AgentRun, user: User) -> None:
    if run.user_id != user.id and user.role != "admin":
        raise HTTPException(NOT_FOUND, "Run not found")  # 404, not 403


async def _build_run_out(db: AsyncSession, run: AgentRun):
    steps = (
        await db.execute(
            select(AgentStep)
            .where(AgentStep.run_id == run.id)
            .order_by(AgentStep.sequence)
        )
    ).scalars().all()
    approvals = (
        await db.execute(
            select(ToolApproval)
            .where(ToolApproval.run_id == run.id)
            .order_by(ToolApproval.created_at.desc())
        )
    ).scalars().all()
    out = AgentRunOut.model_validate(run)
    out.steps = [AgentStepOut.model_validate(s) for s in steps]
    out.approvals = [ToolApprovalOut.model_validate(a) for a in approvals]
    # Persisted tool-call audit trail (full arguments/result) — scoped to the
    # run's assistant message so an admin can inspect exactly what ran.
    if run.message_id is not None:
        tc_rows = (
            await db.execute(
                select(ToolCall)
                .where(ToolCall.message_id == run.message_id)
                .order_by(ToolCall.created_at.asc())
            )
        ).scalars().all()
        out.tool_calls = [ToolCallAuditOut.model_validate(t) for t in tc_rows]
    # Restore the multi-agent graph: prefer the live snapshot, fall back to the
    # static definition (e.g. a run that initialized but never progressed).
    graph = getattr(run, "graph_state", None) or getattr(run, "graph_definition", None)
    out.graph = graph
    return out


@router.get("", response_model=list[AgentRunOut], response_model_exclude_none=True)
async def list_runs(
    conversation_id: uuid.UUID | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AgentRun).order_by(AgentRun.created_at.desc()).limit(50)
    # Always scope to the caller's own runs (admin sees all) — INDEPENDENTLY of
    # the conversation filter. Previously the user_id scope lived in the `else`
    # branch, so ?conversation_id=<another user's conv> leaked that user's runs.
    if user.role != "admin":
        stmt = stmt.where(AgentRun.user_id == user.id)
    if conversation_id is not None:
        stmt = stmt.where(AgentRun.conversation_id == conversation_id)
    runs = (await db.execute(stmt)).scalars().all()

    # Batch-load steps/approvals/tool-calls for the whole page instead of one
    # _build_run_out per run (1 + 50×3 queries → 4 total). This endpoint is
    # polled every ~2s while a run is live, so the N+1 was the dominant cost.
    run_ids = [r.id for r in runs]
    steps_by_run: dict[uuid.UUID, list[AgentStep]] = {}
    approvals_by_run: dict[uuid.UUID, list[ToolApproval]] = {}
    tool_calls_by_msg: dict[uuid.UUID, list[ToolCall]] = {}
    if run_ids:
        step_rows = (
            await db.execute(
                select(AgentStep).where(AgentStep.run_id.in_(run_ids)).order_by(AgentStep.sequence)
            )
        ).scalars().all()
        for s in step_rows:
            steps_by_run.setdefault(s.run_id, []).append(s)
        approval_rows = (
            await db.execute(
                select(ToolApproval)
                .where(ToolApproval.run_id.in_(run_ids))
                .order_by(ToolApproval.created_at.desc())
            )
        ).scalars().all()
        for a in approval_rows:
            approvals_by_run.setdefault(a.run_id, []).append(a)
        msg_ids = [r.message_id for r in runs if r.message_id is not None]
        if msg_ids:
            tc_rows = (
                await db.execute(
                    select(ToolCall)
                    .where(ToolCall.message_id.in_(msg_ids))
                    .order_by(ToolCall.created_at.asc())
                )
            ).scalars().all()
            for t in tc_rows:
                tool_calls_by_msg.setdefault(t.message_id, []).append(t)

    outs = []
    for r in runs:
        out = AgentRunOut.model_validate(r)
        out.steps = [AgentStepOut.model_validate(s) for s in steps_by_run.get(r.id, [])]
        out.approvals = [ToolApprovalOut.model_validate(a) for a in approvals_by_run.get(r.id, [])]
        out.tool_calls = [
            ToolCallAuditOut.model_validate(t) for t in tool_calls_by_msg.get(r.message_id, [])
        ] if r.message_id is not None else []
        graph = getattr(r, "graph_state", None) or getattr(r, "graph_definition", None)
        out.graph = graph
        outs.append(out)
    return outs


@router.get("/{run_id}", response_model=AgentRunOut, response_model_exclude_none=True)
async def get_run(
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await _load_run(db, run_id)
    await _assert_owned(run, user)
    return await _build_run_out(db, run)


@router.post("/{run_id}/approve", response_model_exclude_none=True,
             dependencies=[Depends(rate_limit_user(60, 60, "approval"))])
async def approve_run(
    run_id: uuid.UUID,
    body: ApproveRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await _load_run(db, run_id)
    await _assert_owned(run, user)

    ap = await db.get(ToolApproval, body.approval_id)
    if ap is None or ap.run_id != run.id:
        raise HTTPException(NOT_FOUND, "Approval not found")
    if ap.status != "pending":
        return ActionResult(ok=False, status=ap.status, message="already decided")

    ap.status = "approved"
    ap.approved_by = user.id
    ap.decided_at = datetime.now(timezone.utc)
    # PERSIST FIRST: durable command, then commit, THEN publish the live signal.
    await durable_controls.record_approve(db, run.id, ap.id, user_id=user.id)
    await db.commit()

    # Broadcast the decision across workers (also signals the local coordinator).
    await approval_bus.publish(approval_id=str(ap.id), decision="approved")
    await audit_service.log(
        actor_id=user.id, action="approval:approved",
        target=f"approval:{ap.id}",
        detail={"tool": ap.tool_name, "run_id": str(run.id)},
    )
    return ActionResult(ok=True, status="approved")


@router.post("/{run_id}/reject", response_model_exclude_none=True,
             dependencies=[Depends(rate_limit_user(60, 60, "approval"))])
async def reject_run(
    run_id: uuid.UUID,
    body: RejectRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await _load_run(db, run_id)
    await _assert_owned(run, user)

    ap = await db.get(ToolApproval, body.approval_id)
    if ap is None or ap.run_id != run.id:
        raise HTTPException(NOT_FOUND, "Approval not found")
    if ap.status != "pending":
        return ActionResult(ok=False, status=ap.status, message="already decided")

    ap.status = "rejected"
    ap.reason = body.reason or "rejected by user"
    ap.decided_at = datetime.now(timezone.utc)
    # PERSIST FIRST: durable command, then commit, THEN publish the live signal.
    await durable_controls.record_reject(
        db, run.id, ap.id, reason=ap.reason, user_id=user.id
    )
    await db.commit()

    await approval_bus.publish(approval_id=str(ap.id), decision="rejected", reason=ap.reason)
    await audit_service.log(
        actor_id=user.id, action="approval:rejected",
        target=f"approval:{ap.id}",
        detail={"tool": ap.tool_name, "run_id": str(run.id), "reason": ap.reason},
    )
    return ActionResult(ok=True, status="rejected")


@router.post("/{run_id}/cancel", response_model_exclude_none=True,
             dependencies=[Depends(rate_limit_user(60, 60, "approval"))])
async def cancel_run(
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await _load_run(db, run_id)
    await _assert_owned(run, user)
    if run.status in ("completed", "failed", "cancelled"):
        return ActionResult(ok=False, status=run.status, message="run already finished")

    run.status = "cancelled"
    run.finished_at = datetime.now(timezone.utc)
    # PERSIST FIRST: durable cancel command, then commit, THEN signal the run.
    await durable_controls.record_cancel(db, run.id)
    await db.commit()

    # Signal the live run to stop cooperatively (flips the in-process cancel
    # event the runtime polls between and within streaming rounds).
    ctl = get_run_control(run.id)
    if ctl is not None:
        ctl.cancel.set()

    await approval_bus.publish(approval_id=str(run.id), decision="cancelled")
    return ActionResult(ok=True, status="cancelled")


# --------------------------------------------------------------------------- #
# Phase 2: research-plan + run-control endpoints
# --------------------------------------------------------------------------- #
@router.post("/{run_id}/plan/confirm", response_model_exclude_none=True,
             dependencies=[Depends(rate_limit_user(60, 60, "approval"))])
async def confirm_plan(
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Confirm the run's plan.

    When PLAN_REQUIRE_CONFIRMATION is enabled the run is gated on this status
    (it polls plan_status before executing); when disabled the decision is
    still recorded on the run row for audit.
    """
    run = await _load_run(db, run_id)
    await _assert_owned(run, user)
    run.plan_status = "confirmed"
    await db.commit()
    return ActionResult(ok=True, status="confirmed")


@router.post("/{run_id}/plan/update", response_model_exclude_none=True,
             dependencies=[Depends(rate_limit_user(60, 60, "approval"))])
async def update_plan(
    run_id: uuid.UUID,
    body: PlanUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await _load_run(db, run_id)
    await _assert_owned(run, user)
    plan = dict(run.plan or {})
    revision_notes: list[str] = []
    if body.summary is not None and body.summary != plan.get("summary"):
        plan["summary"] = body.summary
        revision_notes.append(f"计划修订：{body.summary[:160]}")
    if body.steps is not None:
        new_steps = [s.model_dump() for s in body.steps]
        if new_steps != plan.get("steps"):
            plan["steps"] = new_steps
            revision_notes.append(
                f"计划步骤已更新（{len(new_steps)} 步）"
            )
    run.plan = plan
    run.plan_status = "updated"
    await db.commit()
    # A revision on a LIVE run must reach the executor: hand each change to the
    # in-process run control as an appended instruction (the runtimes drain
    # these between stages / tokens) AND persist a durable command so a worker
    # restart can replay it.
    ctl = get_run_control(run.id)
    for note in revision_notes:
        if ctl is not None:
            ctl.add_instruction(note)
        await durable_controls.record_instruction(db, run.id, note)
    await db.commit()
    return ActionResult(ok=True, status="updated")


@router.post("/{run_id}/instructions", response_model_exclude_none=True)
async def append_instruction(
    run_id: uuid.UUID,
    body: RunInstructionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Append mid-run guidance. Stored on the run + handed to the live runtime."""
    run = await _load_run(db, run_id)
    await _assert_owned(run, user)
    # Persist (newest last).
    instructions = list((run.user_instructions or {}).get("items", []))
    instructions.append(body.instruction)
    run.user_instructions = {"items": instructions}
    # PERSIST FIRST: durable instruction command, then commit, THEN hand off.
    await durable_controls.record_instruction(db, run.id, body.instruction)
    await db.commit()
    # Hand to a live run if one is in flight.
    ctl = get_run_control(run.id)
    if ctl is not None:
        ctl.add_instruction(body.instruction)
    return ActionResult(ok=True, status="received")


@router.post("/{run_id}/pause", response_model_exclude_none=True)
async def pause_run(
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await _load_run(db, run_id)
    await _assert_owned(run, user)
    run.paused_at = datetime.now(timezone.utc)
    # PERSIST FIRST: durable pause command, then commit, THEN signal the run.
    await durable_controls.record_pause(db, run.id)
    await db.commit()
    ctl = get_run_control(run.id)
    if ctl is not None:
        ctl.pause()
    return ActionResult(ok=True, status="paused")


@router.post("/{run_id}/resume", response_model_exclude_none=True)
async def resume_run(
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await _load_run(db, run_id)
    await _assert_owned(run, user)
    run.paused_at = None
    # PERSIST FIRST: durable resume command, then commit, THEN signal the run.
    await durable_controls.record_resume(db, run.id)
    await db.commit()
    ctl = get_run_control(run.id)
    if ctl is not None:
        ctl.resume()
    return ActionResult(ok=True, status="resumed")


# --------------------------------------------------------------------------- #
# Task 5: cursor-replay SSE endpoint
# --------------------------------------------------------------------------- #
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
# Event types that mark the end of a run's event stream.
_TERMINAL_EVENT_TYPES = frozenset(
    {"run.completed", "run.failed", "run.cancelled", "done", "error"}
)


def _sse_frame(event_type: str, data: dict, event_id: int | None = None) -> str:
    """Format one SSE frame with optional ``id:`` line (for Last-Event-ID resume)."""
    parts: list[str] = []
    if event_id is not None:
        parts.append(f"id: {event_id}")
    parts.append(f"event: {event_type}")
    parts.append(f"data: {json.dumps(data, default=str, ensure_ascii=False)}")
    return "\n".join(parts) + "\n\n"


async def _run_is_terminal(db: AsyncSession, run_id: uuid.UUID) -> bool:
    """Check whether the run has reached a terminal status."""
    result = await db.execute(
        select(AgentRun.status).where(AgentRun.id == run_id)
    )
    current = result.scalar_one_or_none()
    return current is None or current in _TERMINAL_STATUSES


async def tail_run_events(
    request: Request,
    run_id: uuid.UUID,
    *,
    cursor: int = 0,
    bind: Any = None,
) -> AsyncIterator[str]:
    """Replay durable events from ``cursor`` then tail new events as SSE frames.

    Factored from :func:`stream_run_events` so the durable chat dispatch can
    reuse the exact same cursor-replay + tail loop. READ-ONLY: never executes
    or cancels the run. A client disconnect closes only this subscription.

    ``bind`` is an :class:`~sqlalchemy.ext.asyncio.AsyncEngine` (or compatible
    bind) for the short-lived tail sessions. It defaults to the app's
    ``AsyncSessionLocal`` engine so production works without arguments; tests
    pass the test engine.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    if bind is None:
        from app.db import AsyncSessionLocal as _factory

        bind = _factory.kw["bind"]

    tail_factory = async_sessionmaker(
        bind=bind, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    # Adaptive polling: while events are flowing (an answer is streaming),
    # poll at streaming cadence so tokens reach the client in ~real time
    # (WORKER_POLL_INTERVAL_SECONDS=1s batches a fast code block into big
    # chunks that read as "the code appears only when it finishes"). When
    # idle, back off to the regular worker poll interval.
    stream_interval = max(0.05, getattr(
        get_settings(), "SSE_STREAM_POLL_INTERVAL_SECONDS", 0.15
    ))
    idle_interval = max(stream_interval, get_settings().WORKER_POLL_INTERVAL_SECONDS)
    heartbeat = max(5, get_settings().SSE_HEARTBEAT_SECONDS)

    last_seq = cursor
    last_event_at = asyncio.get_event_loop().time()
    async with tail_factory() as probe:
        already_terminal = await _run_is_terminal(probe, run_id)

    while True:
        if await request.is_disconnected():
            return

        async with tail_factory() as tail_db:
            events = await EventStore(tail_db).replay(
                run_id, after_sequence=last_seq
            )
            is_terminal = await _run_is_terminal(tail_db, run_id)

        terminal_sent = False
        for evt in events:
            last_seq = evt.sequence
            yield _sse_frame(evt.event_type, evt.data, event_id=evt.sequence)
            if evt.event_type in _TERMINAL_EVENT_TYPES:
                terminal_sent = True

        if terminal_sent or (already_terminal and not events):
            return
        if is_terminal and not events:
            return

        # Heartbeat if we've been idle too long.
        now = asyncio.get_event_loop().time()
        if events:
            last_event_at = now
        elif now - last_event_at >= heartbeat:
            yield ": keepalive\n\n"
            last_event_at = now

        # Events are still flowing → fast poll (streaming cadence); idle →
        # the regular worker interval.
        await asyncio.sleep(stream_interval if events else idle_interval)


@router.get("/{run_id}/events")
async def stream_run_events(
    run_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Cursor-replay SSE: replay durable events from ``Last-Event-ID``, then tail.

    This endpoint ONLY READS — it never executes or cancels the run. A client
    disconnect closes the subscription; the run continues on the worker.

    Frame format::

        id: <sequence>
        event: <event_type>
        data: <json>

    The ``Last-Event-ID`` header (set automatically by the browser EventSource
    on reconnect from the last received ``id``) seeds the cursor so a reconnect
    resumes exactly where it left off.
    """
    run = await _load_run(db, run_id)
    await _assert_owned(run, user)

    # Parse the cursor (default 0 = replay from the start).
    try:
        cursor = int(last_event_id) if last_event_id else 0
    except (ValueError, TypeError):
        cursor = 0

    generator = tail_run_events(request, run.id, cursor=cursor, bind=db.bind)
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
