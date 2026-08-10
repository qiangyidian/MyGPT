"""Agent-runs API (Phase 3): inspect a run and drive human-in-the-loop approvals.

  GET    /api/agent-runs                 list runs (optionally by conversation)
  GET    /api/agent-runs/{run_id}        run detail + steps + approvals
  POST   /api/agent-runs/{run_id}/approve   approve a pending dangerous tool
  POST   /api/agent-runs/{run_id}/reject    reject a pending dangerous tool
  POST   /api/agent-runs/{run_id}/cancel    cancel a waiting run

Approve/reject also signal the in-process :class:`ApprovalCoordinator` so a
paused live stream resumes from the exact step. Cancel flips the run to
``cancelled`` and cancels any pending wait. Ownership is enforced (admin may
view/act on any run).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.approval_bus import approval_bus
from app.agents.run_controls import get as get_run_control
from app.agents.workflow import controls as durable_controls
from app.services import audit_service
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
    return [await _build_run_out(db, r) for r in runs]


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
    if body.summary is not None:
        plan["summary"] = body.summary
    if body.steps is not None:
        plan["steps"] = [s.model_dump() for s in body.steps]
    run.plan = plan
    run.plan_status = "updated"
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
