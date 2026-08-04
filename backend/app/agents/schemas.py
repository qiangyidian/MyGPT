"""Unified agent event protocol, turn context, and flow state.

This is the contract every runtime speaks. A runtime consumes an
:class:`AgentTurnContext` and yields :class:`AgentEvent` objects; the
:class:`~app.agents.orchestrator.ChatOrchestrator` forwards each event to the
SSE layer unchanged. Keeping the event vocabulary in one place means the
frontend, the runtimes, and the persisted audit trail (``agent_steps``) all
agree on shapes.

Event vocabulary (the SSE ``event:`` name is ``AgentEvent.kind``):

  * ``run_started``       — a run began (run_id, runtime, conversation/message ids)
  * ``meta``              — backward-compatible message/conversation resolution
  * ``plan_created``      — the agent published a short structured plan
  * ``step_started``      — a plan/agent/review step began
  * ``step_completed``    — a step finished
  * ``tool_call``         — the agent is invoking a tool (id, name, arguments)
  * ``tool_result``       — a tool returned (ok reflects the *real* outcome)
  * ``approval_required`` — a dangerous tool is blocked pending human approval
  * ``agent_graph``      — full multi-agent topology, sent once at run start
  * ``agent_status``     — one agent's status changed (running/completed/…)
  * ``agent_edge``       — a handoff/dependency edge changed status
  * ``run_status``       — overall run status + currently-running agent ids
  * ``token``             — streamed answer text delta
  * ``citations``         — RAG citations for this turn
  * ``done``              — turn finished (finish_reason)
  * ``error``             — turn failed (code, message)

  ``tool_call``/``tool_result`` carry an optional ``agent_id``/``task_id`` so the
  frontend can nest a tool under the agent that invoked it (multi-agent runs).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, Field

from app.providers.base import FinishReason
from app.schemas import ChatRequest, Citation

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids runtime DB imports
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import Conversation, Message, ModelConfig, User


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class RuntimeKind(str, Enum):
    native = "native"
    crewai = "crewai"


class RunStatus(str, Enum):
    pending = "pending"
    running = "running"
    waiting_approval = "waiting_approval"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class StepType(str, Enum):
    plan = "plan"
    llm = "llm"
    tool = "tool"
    review = "review"
    approval = "approval"


class StepStatus(str, Enum):
    pending = "pending"
    running = "running"
    waiting = "waiting"
    done = "done"
    error = "error"


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class ExecutionMode(str, Enum):
    """How the orchestrator should pick a runtime for this turn."""

    auto = "auto"      # router decides (default)
    chat = "chat"      # force native simple chat
    agent = "agent"    # force the agent runtime (CrewAI when available)


# --------------------------------------------------------------------------- #
# Intent recognition (model-driven, context-fed)
#
# Replaces the brittle keyword substring router. Each turn assembles a set of
# typed context fragments (mode / deliverable seed / environment / conversation
# gist / user instructions), feeds them to one lightweight LLM call, and gets
# back a structured IntentDecision that drives routing (native vs research vs
# debate crew) and is surfaced to the main model so it self-judges intent.
# --------------------------------------------------------------------------- #
class IntentDecision(BaseModel):
    """The model's structured judgment of what the user wants this turn.

    Fields are intentionally small + enumerable so the JSON the classifier
    returns is easy to validate and coerce.
    """

    # Which runtime should run. "native" = single agent; the research profiles
    # and "debate" are the multi-agent CrewAI crews.
    route: str = "native"
    # What the user wants delivered — shapes the writer prompt + whether web
    # tools are allowed. "code" forces native (no research-prose crew).
    deliverable_kind: str = "factual"
    # Tools the model thinks are relevant, e.g. ["web_search", "python_exec"].
    # Empty = leave the tool set open to the route's default.
    tool_hints: list[str] = Field(default_factory=list)
    # 0.0..1.0. Below the confidence threshold the caller falls back to the
    # keyword router rather than trusting an unsure judgment.
    confidence: float = 0.5
    # One-line reason — for telemetry / the intent_recognized event.
    rationale: str = ""


def ev_intent_recognized(
    *,
    run_id: uuid.UUID | str,
    route: str,
    deliverable_kind: str,
    confidence: float,
    rationale: str = "",
    tool_hints: list[str] | None = None,
    fragments: list[str] | None = None,
) -> AgentEvent:
    """Tell the client what intent the model recognized for this turn.

    Surfaces the (previously silent) routing decision so the user/UI can see
    WHY a turn went native vs research crew — the antidote to the keyword
    router silently mis-routing (e.g. a code request landing in the research
    pipeline). ``fragments`` lists the context-fragment names that were fed in.
    """
    return AgentEvent(
        kind="intent_recognized",
        data={
            "run_id": str(run_id),
            "route": route,
            "deliverable_kind": deliverable_kind,
            "confidence": confidence,
            "rationale": rationale,
            "tool_hints": tool_hints or [],
            "fragments": fragments or [],
        },
    )


# --------------------------------------------------------------------------- #
# AgentEvent
# --------------------------------------------------------------------------- #
class AgentEvent(BaseModel):
    """One event yielded by a runtime. ``kind`` is the SSE event name."""

    kind: str
    data: dict[str, Any] = Field(default_factory=dict)

    def to_sse_envelope(self) -> dict[str, Any]:
        """Return ``{"event": kind, "data": data}`` — the shape the router emits."""
        return {"event": self.kind, "data": self.data}


# ---- event constructors ----------------------------------------------------
def ev_run_started(
    *, run_id: uuid.UUID | str, runtime: str, conversation_id: uuid.UUID | str, message_id: uuid.UUID | str
) -> AgentEvent:
    return AgentEvent(
        kind="run_started",
        data={
            "run_id": str(run_id),
            "runtime": runtime,
            "conversation_id": str(conversation_id),
            "message_id": str(message_id),
        },
    )


def ev_meta(*, message_id: uuid.UUID | str, conversation_id: uuid.UUID | str) -> AgentEvent:
    return AgentEvent(kind="meta", data={"message_id": str(message_id), "conversation_id": str(conversation_id)})


def ev_runtime_selected(
    *,
    run_id: uuid.UUID | str,
    requested_mode: str,
    effective_mode: str,
    requested_runtime: str,
    effective_runtime: str,
    agent_profile: str,
    multi_agent_requested: bool,
    multi_agent_executed: bool,
    fallback_reason: str | None = None,
    is_demo: bool = False,
) -> AgentEvent:
    """Tell the client which runtime actually ran and whether a multi-agent
    request was honored or fell back. This is what prevents 'fake multi-agent':
    the frontend opens the agent panel ONLY when ``multi_agent_executed`` is true
    and shows a fallback warning when a multi-agent request couldn't run.

    ``is_demo`` is True only when the answer came from the deterministic
    DemoStageExecutor (canned, non-real content). The frontend MUST render a
    persistent '演示模式，内容非真实生成' warning in that case so a demo answer is
    never mistaken for a genuine model reply."""
    return AgentEvent(
        kind="runtime_selected",
        data={
            "run_id": str(run_id),
            "requested_mode": requested_mode,
            "effective_mode": effective_mode,
            "requested_runtime": requested_runtime,
            "effective_runtime": effective_runtime,
            "agent_profile": agent_profile,
            "multi_agent_requested": bool(multi_agent_requested),
            "multi_agent_executed": bool(multi_agent_executed),
            "fallback_reason": fallback_reason,
            "is_demo": bool(is_demo),
        },
    )


def ev_plan_created(*, summary: str, steps: list[dict[str, Any]]) -> AgentEvent:
    return AgentEvent(kind="plan_created", data={"summary": summary, "steps": steps})


def ev_step_started(*, step_id: str, title: str, step_type: str = "llm", agent: str | None = None) -> AgentEvent:
    data: dict[str, Any] = {"step_id": step_id, "title": title, "type": step_type}
    if agent:
        data["agent"] = agent
    return AgentEvent(kind="step_started", data=data)


def ev_step_completed(*, step_id: str, status: str = "done") -> AgentEvent:
    return AgentEvent(kind="step_completed", data={"step_id": step_id, "status": status})


def ev_tool_call(
    *,
    id: str,
    name: str,
    arguments: dict[str, Any],
    dangerous: bool = False,
    approval_id: str | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
) -> AgentEvent:
    data: dict[str, Any] = {"id": id, "name": name, "arguments": arguments, "dangerous": dangerous}
    if approval_id:
        data["approval_id"] = approval_id
    if agent_id:
        data["agent_id"] = agent_id
    if task_id:
        data["task_id"] = task_id
    return AgentEvent(kind="tool_call", data=data)


def ev_tool_result(
    *,
    id: str,
    name: str,
    ok: bool,
    result: Any = None,
    error: str | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
) -> AgentEvent:
    data: dict[str, Any] = {"id": id, "name": name, "ok": ok, "result": result, "error": error}
    if agent_id:
        data["agent_id"] = agent_id
    if task_id:
        data["task_id"] = task_id
    return AgentEvent(kind="tool_result", data=data)


def ev_approval_required(
    *,
    run_id: uuid.UUID | str,
    approval_id: uuid.UUID | str,
    tool_name: str,
    summary: str,
    risk_level: str,
    arguments_preview: dict[str, Any],
) -> AgentEvent:
    return AgentEvent(
        kind="approval_required",
        data={
            "run_id": str(run_id),
            "approval_id": str(approval_id),
            "tool_name": tool_name,
            "summary": summary,
            "risk_level": risk_level,
            "arguments_preview": arguments_preview,
        },
    )


def ev_token(*, delta: str) -> AgentEvent:
    return AgentEvent(kind="token", data={"delta": delta})


def ev_citations(*, citations: list[Citation]) -> AgentEvent:
    return AgentEvent(kind="citations", data={"citations": [c.model_dump(mode="json") for c in citations]})


def ev_done(*, message_id: uuid.UUID | str, finish_reason: FinishReason = "stop") -> AgentEvent:
    return AgentEvent(kind="done", data={"message_id": str(message_id), "finish_reason": finish_reason})


def ev_error(*, code: str, message: str) -> AgentEvent:
    return AgentEvent(kind="error", data={"code": code, "message": message})


# ---- multi-agent graph events (Phase: multi-agent visualization) -----------
def ev_agent_graph(*, run_id: uuid.UUID | str, graph: dict[str, Any]) -> AgentEvent:
    """Send the full topology once at run start. ``graph`` is the public dict
    from :class:`~app.agents.graph.AgentGraph.to_public_dict`."""
    return AgentEvent(kind="agent_graph", data={"run_id": str(run_id), "graph": graph})


def ev_agent_status(
    *,
    run_id: uuid.UUID | str,
    agent_id: str,
    status: str,
    task_title: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_ms: int | None = None,
    output_summary: str | None = None,
    error: str | None = None,
) -> AgentEvent:
    data: dict[str, Any] = {
        "run_id": str(run_id),
        "agent_id": agent_id,
        "status": status,
    }
    if task_title is not None:
        data["task_title"] = task_title
    if started_at is not None:
        data["started_at"] = started_at
    if finished_at is not None:
        data["finished_at"] = finished_at
    if duration_ms is not None:
        data["duration_ms"] = duration_ms
    if output_summary is not None:
        data["output_summary"] = output_summary
    if error is not None:
        data["error"] = error
    return AgentEvent(kind="agent_status", data=data)


def ev_agent_edge(
    *,
    run_id: uuid.UUID | str,
    edge_id: str,
    status: str,
    label: str | None = None,
) -> AgentEvent:
    data: dict[str, Any] = {"run_id": str(run_id), "edge_id": edge_id, "status": status}
    if label is not None:
        data["label"] = label
    return AgentEvent(kind="agent_edge", data=data)


def ev_run_status(
    *,
    run_id: uuid.UUID | str,
    status: str,
    current_agent_ids: list[str] | None = None,
) -> AgentEvent:
    data: dict[str, Any] = {"run_id": str(run_id), "status": status}
    if current_agent_ids is not None:
        data["current_agent_ids"] = current_agent_ids
    return AgentEvent(kind="run_status", data=data)


# ---- Phase 1+: research-plan + run-control events (reserved for deep_research) ----
def ev_research_plan(
    *,
    run_id: uuid.UUID | str,
    status: str = "draft",
    summary: str = "",
    steps: list[dict[str, Any]] | None = None,
    requires_confirmation: bool = True,
    updated: bool = False,
) -> AgentEvent:
    return AgentEvent(
        kind="research_plan_updated" if updated else "research_plan",
        data={
            "run_id": str(run_id),
            "status": status,
            "summary": summary,
            "steps": steps or [],
            "requires_confirmation": requires_confirmation,
        },
    )


def ev_run_instruction_received(
    *, run_id: uuid.UUID | str, instruction: str, acknowledged: bool = True
) -> AgentEvent:
    return AgentEvent(
        kind="run_instruction_received",
        data={"run_id": str(run_id), "instruction": instruction, "acknowledged": acknowledged},
    )


def ev_run_paused(
    *, run_id: uuid.UUID | str, reason: str = "user", paused_at: str | None = None
) -> AgentEvent:
    data: dict[str, Any] = {"run_id": str(run_id), "reason": reason}
    if paused_at is not None:
        data["paused_at"] = paused_at
    return AgentEvent(kind="run_paused", data=data)


def ev_run_resumed(
    *, run_id: uuid.UUID | str, resumed_at: str | None = None
) -> AgentEvent:
    data: dict[str, Any] = {"run_id": str(run_id)}
    if resumed_at is not None:
        data["resumed_at"] = resumed_at
    return AgentEvent(kind="run_resumed", data=data)


# --------------------------------------------------------------------------- #
# Turn context
# --------------------------------------------------------------------------- #
@dataclass
class AgentTurnContext:
    """Everything a runtime needs to execute one user turn.

    ChatService builds this after resolving the conversation/model, running RAG,
    composing the system prompt, loading+trimming history, and creating the
    pending assistant :class:`~app.models.Message` row. The runtime owns the
    model+tool loop and mutates ``assistant_msg.content`` as it streams.
    """

    db: "AsyncSession"
    user: "User"
    conversation: "Conversation"
    model_config: "ModelConfig"
    request: ChatRequest
    user_content: str
    system_prompt: str
    # OpenAI-format messages, system prompt first, already trimmed to the budget.
    messages: list[dict[str, Any]]
    rag_context: str
    citations: list[Citation]
    assistant_msg: "Message"
    run_id: uuid.UUID
    execution_mode: ExecutionMode = ExecutionMode.auto
    agent_profile: str = "general"
    enable_tools: bool = False
    knowledge_base_id: Optional[uuid.UUID] = None
    # Phase 1: the user-facing capability mode the UI sent (auto | search |
    # deep_research | create | data_analysis). Runtimes may read this for telemetry.
    mode: str = "auto"
    # Populated by the orchestrator; runtimes may attach extra bookkeeping here.
    extra: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Tool execution result (returned by ToolGateway)
# --------------------------------------------------------------------------- #
@dataclass
class ToolExecution:
    """Outcome of one tool call through the gateway."""

    ok: bool
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    # success | error | needs_approval | blocked | timeout
    status: str
    result: Any = None
    error: str | None = None
    approval_id: Optional[uuid.UUID] = None
    truncated: bool = False
    latency_ms: int | None = None

    def to_openai_tool_message(self) -> dict[str, Any]:
        """Render as an OpenAI ``tool``-role message for the next model round."""
        if self.ok:
            content = self.result if isinstance(self.result, str) else _stringify(self.result)
        else:
            content = _stringify({"error": self.error or "tool failed"})
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "name": self.tool_name,
            "content": content,
        }


def _stringify(value: Any) -> str:
    import json

    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class ApprovalRequired(Exception):
    """Raised when a dangerous tool call has no valid approval.

    Phase 0: the gateway returns a ``needs_approval`` :class:`ToolExecution`
    instead of raising, so the agent loop can emit the event and continue.
    Phase 3 resume path raises this to pause the flow.
    """

    def __init__(
        self,
        *,
        approval_id: uuid.UUID,
        tool_name: str,
        arguments: dict[str, Any],
        risk_level: str,
        summary: str,
    ) -> None:
        self.approval_id = approval_id
        self.tool_name = tool_name
        self.arguments = arguments
        self.risk_level = risk_level
        self.summary = summary
        super().__init__(f"approval required for tool {tool_name!r}")


class BudgetExceeded(Exception):
    """Raised when an agent run crosses a hard stop (steps/tools/time/tokens)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# --------------------------------------------------------------------------- #
# Flow state (Phase 2/3) — persisted via agent_runs + conversation_memories
# --------------------------------------------------------------------------- #
class ConversationFlowState(BaseModel):
    """Structured, cross-turn state for an agent flow.

    Mirrors the plan's ``ConversationFlowState``. ``recent_messages`` and the
    summary together form the rolling context; ``plan``/``completed_steps``/
    ``pending_steps`` track task progress across turns; ``pending_approval``
    captures an in-flight human gate so a resumed run knows where to continue.
    """

    conversation_id: str
    user_id: str
    turn_id: str = ""
    user_goal: str = ""
    intent: str = "chat"  # chat | knowledge | deep_research | action
    recent_messages: list[dict[str, Any]] = Field(default_factory=list)
    conversation_summary: str = ""
    long_term_facts: list[dict[str, Any]] = Field(default_factory=list)
    plan: list[dict[str, Any]] = Field(default_factory=list)
    completed_steps: list[str] = Field(default_factory=list)
    pending_steps: list[str] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls_used: int = 0
    token_budget_used: int = 0
    pending_approval: dict[str, Any] | None = None
    final_answer: str = ""
