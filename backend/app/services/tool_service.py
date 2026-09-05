"""Tool listing + ad-hoc tool testing.

Both go through ``get_default_registry()`` so the set of tools has one source of
truth. The agent loop uses the same registry at chat time.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from app.agents.policies.tool_policy import is_tool_allowed
from app.schemas import ToolInfo, ToolTestResult
from app.tools.base import ToolError
from app.tools.registry_init import get_default_registry

if TYPE_CHECKING:  # pragma: no cover
    from app.models.user import User


def list_tools() -> list[ToolInfo]:
    """Expose every registered tool with its OpenAI-style parameter list."""
    registry = get_default_registry()
    out: list[ToolInfo] = []
    for tool in registry.list():
        out.append(
            ToolInfo(
                name=tool.name,
                description=tool.description,
                category=getattr(tool, "category", "general"),
                dangerous=getattr(tool, "dangerous", False),
                parameters=[
                    {
                        "name": p.name,
                        "type": p.type,
                        "description": p.description,
                        "required": p.required,
                        **({"default": p.default} if p.default is not None else {}),
                        **({"enum": p.enum} if p.enum else {}),
                    }
                    for p in tool.parameters
                ],
            )
        )
    return out


async def test_tool(
    name: str, arguments: dict, user: User | None = None
) -> ToolTestResult:
    """Run a tool with the given args and report success/latency/error.

    Safety: the env permission gate the :class:`ToolGateway` uses is applied
    here too (step 2 of the gateway), so ``python_exec`` cannot be RCE'd through
    this ad-hoc endpoint in production. Dangerous tools also self-guard inside
    their ``run()``. Safe tools (http_get / web_search / file_analyze /
    datetime_now) and the read-only-hardened db_query remain testable.
    """
    registry = get_default_registry()
    # Env permission gate — mainly blocks python_exec outside dev/test.
    if not is_tool_allowed(name, user):
        return ToolTestResult(
            ok=False,
            result=None,
            error=f"tool {name!r} is not permitted in this environment",
            latency_ms=0,
        )
    start = time.perf_counter()
    try:
        tool = registry.get(name)
        result = await tool.run(**(arguments or {}))
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ToolTestResult(ok=True, result=result, error=None, latency_ms=latency_ms)
    except ToolError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ToolTestResult(ok=False, result=None, error=str(exc), latency_ms=latency_ms)
    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ToolTestResult(ok=False, result=None, error=f"{type(exc).__name__}: {exc}", latency_ms=latency_ms)
