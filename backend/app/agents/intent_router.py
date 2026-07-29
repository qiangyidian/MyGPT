"""Intent Router: maps a user-facing capability ``mode`` to a concrete
execution route (runtime / agent profile / tools).

This centralizes the decision that was previously scattered across
``ChatOrchestrator._select_runtime`` (mode only) and
``CrewAIRuntime.stream_turn`` (intent + profile). The UI never sends internal
runtime enums anymore — it sends one of the stable ``mode`` values below, and
the router turns it into the execution_mode / agent_profile / tool allowlist
the orchestrator and runtime already understand.

User-facing modes (the only thing the UI exposes):
  auto | search | deep_research | create | data_analysis

Legacy ``execution_mode``/``agent_profile`` on the request still override when
a caller sets them explicitly (backward compatibility for existing clients and
tests); see :func:`apply_route` in ``chat_service``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.agents.schemas import ExecutionMode

# The stable wire enum the frontend sends. Kept here so backend + tests agree.
VALID_MODES = {"auto", "search", "deep_research", "create", "data_analysis"}

# Tools considered "web" — disabled in create mode to keep it focused.
_WEB_TOOLS = {"web_search", "http_get"}


@dataclass
class RouteDecision:
    """The resolved execution plan for one user turn."""

    execution_mode: ExecutionMode = ExecutionMode.auto
    agent_profile: str = "general"
    enable_tools: bool = False
    # When True, the CrewAI runtime may run the multi-agent graph.
    use_multi_agent: bool = False
    # None = all tools; otherwise restrict to this allowlist.
    tool_allowlist: Optional[list[str]] = None
    # When True, exclude web tools even if otherwise allowed (create mode).
    disable_web: bool = False
    # The original user-facing mode (for telemetry / metadata only).
    mode: str = "auto"


def decide_route(
    mode: str,
    *,
    has_knowledge_base: bool = False,
    has_attachment: bool = False,
) -> RouteDecision:
    """Resolve a user-facing ``mode`` into a :class:`RouteDecision`."""
    m = (mode or "auto").strip().lower()
    if m not in VALID_MODES:
        m = "auto"

    if m == "search":
        # Web-first, native multi-turn tool loop. The user never sees "Native".
        return RouteDecision(
            execution_mode=ExecutionMode.auto,
            agent_profile="general",
            enable_tools=True,
            use_multi_agent=False,
            tool_allowlist=["web_search", "http_get"],
            mode=m,
        )

    if m == "deep_research":
        # CrewAI multi-agent. parallel_research when a KB is attached so both
        # the web and knowledge lines run; otherwise the sequential crew.
        profile = "parallel_research" if has_knowledge_base else "deep_research"
        return RouteDecision(
            execution_mode=ExecutionMode.agent,
            agent_profile=profile,
            enable_tools=True,
            use_multi_agent=True,
            mode=m,
        )

    if m == "create":
        # Long-form writing/rewrite/summary. No web; stays native.
        return RouteDecision(
            execution_mode=ExecutionMode.auto,
            agent_profile="general",
            enable_tools=False,
            use_multi_agent=False,
            disable_web=True,
            mode=m,
        )

    if m == "data_analysis":
        # File analysis (+ python sandbox when enabled). Native tool loop.
        # Allowlist left open: the registry decides what's actually available.
        return RouteDecision(
            execution_mode=ExecutionMode.auto,
            agent_profile="general",
            enable_tools=True,
            use_multi_agent=False,
            mode=m,
        )

    # auto: router decides. Default to native simple chat, tools off. This
    # preserves the pre-Phase-1 default behaviour.
    return RouteDecision(mode="auto")


def filter_tool_names(
    names: list[str], route: RouteDecision
) -> list[str]:
    """Apply the route's allowlist / disable_web to a list of tool names."""
    out = list(names)
    if route.tool_allowlist is not None:
        allow = set(route.tool_allowlist)
        out = [n for n in out if n in allow]
    if route.disable_web:
        out = [n for n in out if n not in _WEB_TOOLS]
    return out
