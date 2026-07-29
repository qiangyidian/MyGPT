"""Schemas for the agent-runs API (Phase 3): run detail + approval actions."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class AgentStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sequence: int
    step_type: str
    agent_name: str
    agent_id: str = ""
    task_id: str = ""
    tool_name: str
    status: str
    input_redacted: Optional[dict[str, Any]] = None
    output_redacted: Optional[dict[str, Any]] = None
    latency_ms: Optional[int] = None
    created_at: datetime


class ToolApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    run_id: uuid.UUID
    tool_name: str
    arguments: dict[str, Any]
    risk_level: str
    status: str
    reason: Optional[str] = None
    created_at: datetime
    expires_at: Optional[datetime] = None


class ToolCallAuditOut(BaseModel):
    """A persisted tool call's full input/output — the on-prem audit surface.

    Unlike AgentStep (which stores redacted previews), this carries the real
    arguments/result so an admin can answer 'what did the agent do to my data?'.
    """
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    tool_name: str
    arguments: dict[str, Any]
    result: Optional[dict[str, Any]] = None
    status: str
    error_message: Optional[str] = None
    created_at: datetime


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: Optional[uuid.UUID] = None
    runtime: str
    flow_name: str
    status: str
    current_step: str
    input: dict[str, Any]
    output: Optional[dict[str, Any]] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error_message: Optional[str] = None
    created_at: datetime
    steps: list[AgentStepOut] = []
    approvals: list[ToolApprovalOut] = []
    # Persisted tool-call audit trail (full arguments/result) for this run.
    tool_calls: list[ToolCallAuditOut] = []
    # Multi-agent graph snapshot (None for single-agent / native runs). The live
    # state is preferred; the static definition is the fallback. The frontend
    # uses this to restore the panel after a page refresh.
    graph: Optional[dict[str, Any]] = None
    # ---- Phase 1+: research plan + run-time instructions (reserved) ----
    plan: Optional[dict[str, Any]] = None
    plan_status: Optional[str] = None
    user_instructions: Optional[dict[str, Any]] = None
    paused_at: Optional[datetime] = None


class ApproveRequest(BaseModel):
    approval_id: uuid.UUID


class RejectRequest(BaseModel):
    approval_id: uuid.UUID
    reason: Optional[str] = None


class ActionResult(BaseModel):
    ok: bool
    status: str
    message: Optional[str] = None


# ---- Phase 1+: research-plan + run-control request schemas (reserved) -------
class PlanStepIn(BaseModel):
    id: str
    title: str
    description: str = ""
    sources: list[str] = []


class PlanUpdateRequest(BaseModel):
    summary: Optional[str] = None
    steps: Optional[list[PlanStepIn]] = None


class RunInstructionRequest(BaseModel):
    instruction: str

