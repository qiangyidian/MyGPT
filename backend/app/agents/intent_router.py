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
# The UI picker now exposes only `speed` and `expert`; the rest remain valid for
# backward compatibility (older clients, internal escalation, tests).
VALID_MODES = {
    "speed", "expert", "hermes",
    "auto", "search", "deep_research", "create", "data_analysis", "debate",
}

# Tools considered "web" — disabled in create mode to keep it focused.
_WEB_TOOLS = {"web_search", "http_get"}

# Minimum length for auto-mode intent-driven multi-agent escalation, so trivial
# one-liners ("分析下", "总结下") stay native. Lower = more aggressive.
_AUTO_MULTI_MIN_LEN = 6

# Below this confidence the model's intent judgment is NOT trusted — fall back to
# the keyword router rather than act on an unsure classification.
_INTENT_MIN_CONFIDENCE = 0.5


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
    # The effective user-facing mode after intent-aware adjustment (the value
    # downstream code / telemetry should use).
    mode: str = "auto"
    # The mode the user actually selected (may differ from `mode` when a
    # deep_research request is detected as pure code-generation and rerouted).
    requested_mode: str = "auto"


def decide_route(
    mode: str,
    *,
    has_knowledge_base: bool = False,
    has_attachment: bool = False,
    user_content: str = "",
) -> RouteDecision:
    """Resolve a user-facing ``mode`` into a :class:`RouteDecision`."""
    m = (mode or "speed").strip().lower()
    if m not in VALID_MODES:
        m = "speed"

    if m == "speed":
        # 极速模式：单 Agent 原生直答，不启动多 Agent、不跑意图分类（首 token 最快）。
        # 允许联网搜索（web_search / http_get）——这样极速提问也能有「来源」，
        # 但只走原生工具循环，绝不拉起多 Agent crew。
        return RouteDecision(
            execution_mode=ExecutionMode.auto,
            agent_profile="general",
            enable_tools=True,
            use_multi_agent=False,
            tool_allowlist=["web_search", "http_get"],
            mode="speed",
            requested_mode="speed",
        )

    if m == "hermes":
        # Hermes 模式：整段透传给 Hermes Agent（服务端执行全部 27 个工具集：
        # 联网搜索/浏览器自动化/Terminal/文件/多模态/子任务委派等）。
        # 本地不启动任何工具（本地 schema 不发给 Hermes），不加 RAG 注入 ——
        # Hermes 有自己的记忆与检索；首 token 后由 Hermes 服务端自主工作。
        return RouteDecision(
            execution_mode=ExecutionMode.auto,
            agent_profile="general",
            enable_tools=False,
            use_multi_agent=False,
            mode="hermes",
            requested_mode="hermes",
        )

    if m == "expert":
        # 专家模式：默认使用多 Agent 协作（研究 crew；绑了知识库走并行 research）。
        # 代码请求例外 —— 研究 Writer 会截断代码，所以代码仍走原生（与 deep_research 一致）。
        from app.agents.planning import looks_like_code_request

        if looks_like_code_request(user_content):
            return RouteDecision(
                execution_mode=ExecutionMode.auto,
                agent_profile="general",
                enable_tools=False,
                use_multi_agent=False,
                disable_web=True,
                mode="create",
                requested_mode="expert",
            )
        profile = "parallel_research" if has_knowledge_base else "deep_research"
        return RouteDecision(
            execution_mode=ExecutionMode.agent,
            agent_profile=profile,
            enable_tools=True,
            use_multi_agent=True,
            mode="expert",
            requested_mode="expert",
        )

    if m == "search":
        # Web-first, native multi-turn tool loop. The user never sees "Native".
        return RouteDecision(
            execution_mode=ExecutionMode.auto,
            agent_profile="general",
            enable_tools=True,
            use_multi_agent=False,
            tool_allowlist=["web_search", "http_get"],
            mode=m,
            requested_mode=m,
        )

    if m == "deep_research":
        # A pure "generate code" request does not belong in the research pipeline
        # (the research Writer caps output and writes prose, truncating code).
        # Reroute it to native create — observable via requested_mode != mode.
        # "research then code" requests (looks_like_research_then_code) stay.
        from app.agents.planning import looks_like_code_request

        if looks_like_code_request(user_content):
            return RouteDecision(
                execution_mode=ExecutionMode.auto,
                agent_profile="general",
                enable_tools=False,
                use_multi_agent=False,
                disable_web=True,
                mode="create",
                requested_mode="deep_research",
            )
        # CrewAI multi-agent. parallel_research when a KB is attached so both
        # the web and knowledge lines run; otherwise the sequential crew.
        profile = "parallel_research" if has_knowledge_base else "deep_research"
        return RouteDecision(
            execution_mode=ExecutionMode.agent,
            agent_profile=profile,
            enable_tools=True,
            use_multi_agent=True,
            mode=m,
            requested_mode=m,
        )

    if m == "debate":
        # Real multi-agent debate: advocate-a ‖ advocate-b → judge. CrewAI
        # availability is enforced by the orchestrator — if the multi-agent
        # runtime is unavailable this fails loudly (with an observable fallback
        # reason), it NEVER silently degrades to a single model role-playing.
        return RouteDecision(
            execution_mode=ExecutionMode.agent,
            agent_profile="debate",
            enable_tools=False,  # debate is structured argumentation, not tool use
            use_multi_agent=True,
            disable_web=True,
            mode=m,
            requested_mode=m,
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
            requested_mode=m,
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
            requested_mode=m,
        )

    # auto: inspect the request for an explicit multi-agent / debate intent
    # BEFORE defaulting to plain native chat. This is what makes "use multiple
    # agents to debate X vs Y" actually run a real multi-agent flow instead of
    # being answered by a single model role-playing several agents.
    from app.agents.planning import (
        classify_intent,
        looks_like_debate_request,
        looks_like_multi_agent_request,
    )

    if looks_like_debate_request(user_content):
        return RouteDecision(
            execution_mode=ExecutionMode.agent,
            agent_profile="debate",
            enable_tools=False,
            use_multi_agent=True,
            disable_web=True,
            mode="debate",
            requested_mode="auto",
        )
    if looks_like_multi_agent_request(user_content):
        # Explicit multi-agent ask without a clear two-sided debate → real
        # research crew (NOT native role-play).
        profile = "parallel_research" if has_knowledge_base else "deep_research"
        return RouteDecision(
            execution_mode=ExecutionMode.agent,
            agent_profile=profile,
            enable_tools=True,
            use_multi_agent=True,
            mode="deep_research",
            requested_mode="auto",
        )

    # auto: intent-driven escalation. A research / compare / analyze / summary
    # flavored question (non-trivial length) escalates to the REAL research crew
    # — not only explicit "多Agent" keywords. This is the "less conservative"
    # lever; tune _AUTO_MULTI_MIN_LEN to make it more or less aggressive.
    if (
        len(user_content.strip()) >= _AUTO_MULTI_MIN_LEN
        and classify_intent(user_content) == "deep_research"
    ):
        profile = "parallel_research" if has_knowledge_base else "deep_research"
        return RouteDecision(
            execution_mode=ExecutionMode.agent,
            agent_profile=profile,
            enable_tools=True,
            use_multi_agent=True,
            mode="deep_research",
            requested_mode="auto",
        )

    # auto: router decides. Default to native simple chat, tools off. This
    # preserves the pre-Phase-1 default behaviour.
    return RouteDecision(mode="auto", requested_mode="auto")


def decide_route_with_intent(
    mode: str,
    *,
    user_content: str,
    intent: object | None,
    has_knowledge_base: bool = False,
    has_attachment: bool = False,
) -> RouteDecision:
    """Intent-driven routing — the model-judgment replacement for the keyword
    router.

    If ``intent`` is a trusted :class:`~app.agents.schemas.IntentDecision`
    (present, above the confidence floor, with a valid route), build the
    :class:`RouteDecision` from it. Otherwise fall back to :func:`decide_route`
    (the keyword router) so intent recognition is an enhancement, never a hard
    dependency.

    ``speed`` is special: it ALWAYS stays native single-agent (the user picked
    极速 on purpose) — any intent judgment is ignored.
    """
    if (mode or "").strip().lower() == "speed":
        return decide_route(
            "speed",
            has_knowledge_base=has_knowledge_base,
            has_attachment=has_attachment,
            user_content=user_content,
        )

    from app.agents.schemas import IntentDecision

    trusted = (
        isinstance(intent, IntentDecision)
        and intent.confidence >= _INTENT_MIN_CONFIDENCE
    )
    if not trusted:
        return decide_route(
            mode,
            has_knowledge_base=has_knowledge_base,
            has_attachment=has_attachment,
            user_content=user_content,
        )

    kind = intent.deliverable_kind
    route_name = intent.route

    # Code → native + no web, regardless of the suggested route. This is the
    # direct fix for the "贪吃蛇" class of bug (code landing in the research crew).
    if kind == "code":
        return RouteDecision(
            execution_mode=ExecutionMode.auto,
            agent_profile="general",
            enable_tools=False,
            use_multi_agent=False,
            disable_web=True,
            mode="create",
            requested_mode=mode,
        )

    if route_name == "debate":
        return RouteDecision(
            execution_mode=ExecutionMode.agent,
            agent_profile="debate",
            enable_tools=False,
            use_multi_agent=True,
            disable_web=True,
            mode="debate",
            requested_mode=mode,
        )

    if route_name in ("deep_research", "parallel_research"):
        profile = "parallel_research" if has_knowledge_base else "deep_research"
        return RouteDecision(
            execution_mode=ExecutionMode.agent,
            agent_profile=profile,
            enable_tools=True,
            use_multi_agent=True,
            mode="deep_research",
            requested_mode=mode,
        )

    # native / factual: enable only the tools the model hinted at (if any).
    hints = list(getattr(intent, "tool_hints", []) or [])
    return RouteDecision(
        execution_mode=ExecutionMode.auto,
        agent_profile="general",
        enable_tools=bool(hints),
        use_multi_agent=False,
        tool_allowlist=hints or None,
        mode=mode,
        requested_mode=mode,
    )


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
