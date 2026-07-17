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


class ApproveRequest(BaseModel):
    approval_id: uuid.UUID


class RejectRequest(BaseModel):
    approval_id: uuid.UUID
    reason: Optional[str] = None


class ActionResult(BaseModel):
    ok: bool
    status: str
    message: Optional[str] = None
