"""Runtime abstraction. A runtime consumes an :class:`AgentTurnContext` and
yields :class:`AgentEvent` objects. Two implementations live alongside:

  * :class:`~app.agents.runtime.native_runtime.NativeChatRuntime` — the existing
    model<->tool loop, hardened.
  * :class:`~app.agents.runtime.crewai_runtime.CrewAIRuntime` — a CrewAI Flow
    runtime (imported lazily; optional dependency).
"""
from __future__ import annotations

from typing import AsyncIterator, Protocol

from app.agents.schemas import AgentEvent, AgentTurnContext


class AgentRuntime(Protocol):
    """One agent execution strategy.

    ``stream_turn`` is an async generator. It must terminate by yielding
    exactly one terminal event — ``done`` or ``error`` — after which the
    orchestrator finalizes the run. Non-terminal events (``token``,
    ``tool_call``, ``tool_result``, ``plan_created``, …) may be interleaved.
    """

    name: str

    def stream_turn(self, ctx: AgentTurnContext) -> AsyncIterator[AgentEvent]: ...
