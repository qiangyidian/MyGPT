"""Map CrewAI streaming events onto :class:`AgentEvent`.

CrewAI's event shapes vary across versions; these mappers are intentionally
defensive — they accept either an object with ``.type``/payload attributes or a
plain dict, and return an :class:`AgentEvent` or ``None`` (``None`` == ignore).
Unknown events are ignored so a future CrewAI release cannot break the stream.
"""
from __future__ import annotations

from typing import Any

from app.agents.schemas import AgentEvent, ev_token, ev_tool_call, ev_tool_result


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _etype(obj: Any) -> str:
    t = _get(obj, "type", "")
    return str(t or "").lower()


def map_crewai_event(ev: Any) -> AgentEvent | None:
    """Translate one CrewAI stream event into an AgentEvent, or None to skip."""
    t = _etype(ev)

    # LLM token streaming.
    if any(k in t for k in ("token", "llm_stream", "chunk", "streaming", "text")):
        delta = (
            _get(ev, "content")
            or _get(ev, "text")
            or _get(ev, "delta")
            or _get(ev, "token")
            or _get(ev, "chunk")
            or ""
        )
        if delta:
            return ev_token(delta=str(delta))
        return None

    # Tool usage / result.
    if "tool_usage" in t or t == "using tool":
        name = _get(ev, "tool_name") or _get(ev, "name") or _get(ev, "tool") or ""
        args = _get(ev, "arguments") or _get(ev, "input") or {}
        return ev_tool_call(
            id=str(_get(ev, "id", "crewai") or "crewai"),
            name=str(name),
            arguments=args if isinstance(args, dict) else {},
        )
    if "tool_result" in t:
        name = _get(ev, "tool_name") or _get(ev, "name") or ""
        result = _get(ev, "result") or _get(ev, "output")
        ok = _get(ev, "ok", True)
        return ev_tool_result(
            id=str(_get(ev, "id", "crewai") or "crewai"),
            name=str(name),
            ok=bool(ok),
            result=result,
        )

    return None
