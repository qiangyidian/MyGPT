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

from app.agents.approval_coordinator import approval_coordinator
from app.core.deps import get_current_user
from app.db import get_db
from app.models import AgentRun, AgentStep, ToolApproval, User
from app.schemas import (
    AgentRunOut,
    AgentStepOut,
    ApproveRequest,
    ActionResult,
    RejectRequest,
    ToolApprovalOut,
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
    return out


@router.get("", response_model_exclude_none=True)
async def list_runs(
    conversation_id: uuid.UUID | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AgentRun).order_by(AgentRun.created_at.desc()).limit(50)
    if conversation_id is not None:
        stmt = stmt.where(AgentRun.conversation_id == conversation_id)
    else:
        # Without a conversation filter, scope to the caller's own runs (admin sees all).
        if user.role != "admin":
            stmt = stmt.where(AgentRun.user_id == user.id)
    runs = (await db.execute(stmt)).scalars().all()
    return [await _build_run_out(db, r) for r in runs]


@router.get("/{run_id}", response_model_exclude_none=True)
async def get_run(
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await _load_run(db, run_id)
    await _assert_owned(run, user)
    return await _build_run_out(db, run)


@router.post("/{run_id}/approve", response_model_exclude_none=True)
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
    await db.commit()

    # Wake the paused stream (no-op if nothing is waiting).
    approval_coordinator.approve(ap.id)
    return ActionResult(ok=True, status="approved")


@router.post("/{run_id}/reject", response_model_exclude_none=True)
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
    await db.commit()

    approval_coordinator.reject(ap.id, ap.reason)
    return ActionResult(ok=True, status="rejected")


@router.post("/{run_id}/cancel", response_model_exclude_none=True)
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
    await db.commit()

    approval_coordinator.cancel_run(run.id)
    return ActionResult(ok=True, status="cancelled")
