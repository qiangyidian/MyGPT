"""Tool listing + ad-hoc tool testing (``POST /api/tools/test``).

Both go through ``get_default_registry()`` so the set of tools has one source of
truth. The agent loop uses the same registry at chat time.

The ad-hoc endpoint is a debugging surface, not a second execution engine, so
:func:`test_tool` applies the same gates the
:class:`~app.agents.gateway.tool_gateway.ToolGateway` does — environment
permission, explicit test opt-in, tenant scope — while the route adds auth + a
per-user rate limit (``app/api/tools.py``). Every outcome is audited, because a
call made here never produces the gateway's ``ToolCall`` row.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from app.agents.policies.tool_policy import is_tool_allowed
from app.schemas import ToolInfo, ToolTestResult
from app.tools.base import ToolError
from app.tools.context import bind_tool_context, make_tool_context, reset_tool_context
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


def _is_admin(user: User | None) -> bool:
    return bool(user is not None and getattr(user, "role", "") == "admin")


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


async def test_tool(
    name: str, arguments: dict, user: User | None = None
) -> ToolTestResult:
    """Run a tool with the given args and report success/latency/error.

    Never raises: every failure mode becomes an ``ok=False`` result, and every
    outcome — including the refusals — is audited.
    """
    result = await _test_tool_inner(name, arguments, user)
    await _audit(name, arguments, user=user, ok=result.ok, error=result.error)
    return result


async def _test_tool_inner(
    name: str, arguments: dict, user: User | None
) -> ToolTestResult:
    """The gate sequence. Kept separate so ``test_tool`` can audit uniformly.

    1. **Environment gate** — the same ``is_tool_allowed`` check the gateway
       applies, so ``python_exec`` cannot be RCE'd through this endpoint in
       production and ``db_query`` stays admin-only outside dev.
    2. **Explicit test allow-list** — a tool must opt in via ``user_testable``
       to be callable by a logged-in non-admin. Anything not deliberately marked
       safe (code/SQL execution, workspace tools, MCP wrappers) is refused;
       admins may test whatever the environment gate also allows. New tools
       therefore cannot widen this surface by accident.
    3. **Tenant scope** — the caller's ``user`` is bound as the tool execution
       context, so an id-taking tool (``file_analyze``) resolves the row against
       *this* user instead of trusting the id argument; a ``requires_user`` tool
       with no principal is refused before it runs.
    """
    registry = get_default_registry()
    # 1. Env permission gate — mainly blocks python_exec outside dev and
    #    non-admin db_query outside dev.
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
    except ToolError as exc:
        return ToolTestResult(
            ok=False, result=None, error=str(exc), latency_ms=_elapsed_ms(start)
        )

    # 2. Explicit opt-in gate.
    if not getattr(tool, "user_testable", False) and not _is_admin(user):
        return ToolTestResult(
            ok=False,
            result=None,
            error=f"tool {name!r} 未开放给用户直接测试 (仅管理员可用)",
            latency_ms=_elapsed_ms(start),
        )

    # 3. Tenant scope, fail closed.
    if getattr(tool, "requires_user", False) and user is None:
        return ToolTestResult(
            ok=False,
            result=None,
            error=f"tool {name!r} requires an authenticated user scope",
            latency_ms=_elapsed_ms(start),
        )

    token = bind_tool_context(make_tool_context(user))
    try:
        result = await tool.run(**(arguments or {}))
    except ToolError as exc:
        return ToolTestResult(
            ok=False, result=None, error=str(exc), latency_ms=_elapsed_ms(start)
        )
    except Exception as exc:
        return ToolTestResult(
            ok=False,
            result=None,
            error=f"{type(exc).__name__}: {exc}",
            latency_ms=_elapsed_ms(start),
        )
    finally:
        reset_tool_context(token)

    return ToolTestResult(
        ok=True, result=result, error=None, latency_ms=_elapsed_ms(start)
    )


async def _audit(
    name: str, arguments: dict, *, user: User | None, ok: bool, error: str | None
) -> None:
    """Audit one ad-hoc execution. ``audit_service.log`` never raises into us."""
    from app.agents.policies import preview
    from app.services import audit_service

    await audit_service.log(
        actor_id=getattr(user, "id", None),
        action="tool_test",
        target=name,
        detail={"ok": ok, "error": error, "arguments": preview(arguments or {})},
    )


__all__ = ["list_tools", "test_tool"]
