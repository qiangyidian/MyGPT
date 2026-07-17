"""Tool listing + ad-hoc tool testing.

Both go through ``get_default_registry()`` so the set of tools has one source of
truth. The agent loop uses the same registry at chat time.
"""
from __future__ import annotations

import time

from app.schemas import ToolInfo, ToolTestResult
from app.tools.base import ToolError
from app.tools.registry_init import get_default_registry


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


async def test_tool(name: str, arguments: dict) -> ToolTestResult:
    """Run a tool with the given args and report success/latency/error."""
    registry = get_default_registry()
    start = time.perf_counter()
    try:
        tool = registry.get(name)
        result = await tool.run(**(arguments or {}))
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ToolTestResult(ok=True, result=result, error=None, latency_ms=latency_ms)
    except ToolError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ToolTestResult(ok=False, result=None, error=str(exc), latency_ms=latency_ms)
    except Exception as exc:  # noqa: BLE001 — surface any failure
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ToolTestResult(ok=False, result=None, error=f"{type(exc).__name__}: {exc}", latency_ms=latency_ms)
