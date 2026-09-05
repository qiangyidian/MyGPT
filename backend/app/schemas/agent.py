"""Schemas for the agent-runs API (Phase 3): run detail + approval actions."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

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
    input_redacted: dict[str, Any] | None = None
    output_redacted: dict[str, Any] | None = None
    latency_ms: int | None = None
    created_at: datetime


class ToolApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    run_id: uuid.UUID
    tool_name: str
    arguments: dict[str, Any]
    risk_level: str
    status: str
    reason: str | None = None
    created_at: datetime
    expires_at: datetime | None = None


class ToolCallAuditOut(BaseModel):
    """A persisted tool call's full input/output — the on-prem audit surface.

    Unlike AgentStep (which stores redacted previews), this carries the real
    arguments/result so an admin can answer 'what did the agent do to my data?'.
    """
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None = None
    status: str
    error_message: str | None = None
    created_at: datetime


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID | None = None
    runtime: str
    flow_name: str
    status: str
    current_step: str
    input: dict[str, Any]
    output: dict[str, Any] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None
    created_at: datetime
    steps: list[AgentStepOut] = []
    approvals: list[ToolApprovalOut] = []
    # Persisted tool-call audit trail (full arguments/result) for this run.
    tool_calls: list[ToolCallAuditOut] = []
    # Multi-agent graph snapshot (None for single-agent / native runs). The live
    # state is preferred; the static definition is the fallback. The frontend
    # uses this to restore the panel after a page refresh.
    graph: dict[str, Any] | None = None
    # ---- Phase 1+: research plan + run-time instructions (reserved) ----
    plan: dict[str, Any] | None = None
    plan_status: str | None = None
    user_instructions: dict[str, Any] | None = None
    paused_at: datetime | None = None


class ApproveRequest(BaseModel):
    approval_id: uuid.UUID


class RejectRequest(BaseModel):
    approval_id: uuid.UUID
    reason: str | None = None


class ActionResult(BaseModel):
    ok: bool
    status: str
    message: str | None = None


# ---- Phase 1+: research-plan + run-control request schemas (reserved) -------
class PlanStepIn(BaseModel):
    id: str
    title: str
    description: str = ""
    sources: list[str] = []


class PlanUpdateRequest(BaseModel):
    summary: str | None = None
    steps: list[PlanStepIn] | None = None


class RunInstructionRequest(BaseModel):
    instruction: str

