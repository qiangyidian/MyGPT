"""Tools router: list available tools + ad-hoc test execution.

Both go through the default registry, which is also what the chat agent loop uses.

``POST /test`` is a debugging surface, so it is locked down on the way in as well
as inside :func:`app.services.tool_service.test_tool`: login required, per-user
rate limited, and only tools that opted into ``user_testable`` (or an admin
caller) may run. The caller's identity is bound as the tool's tenant scope, so
an id-taking tool cannot be pointed at another user's rows.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.deps import get_current_user
from app.core.rate_limit import rate_limit_user
from app.models import User
from app.schemas import ToolInfo, ToolTestRequest, ToolTestResult
from app.services import tool_service

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("", response_model=list[ToolInfo])
async def list_tools(
    user: User = Depends(get_current_user),
) -> list[ToolInfo]:
    return tool_service.list_tools()


@router.post(
    "/test",
    response_model=ToolTestResult,
    # Ad-hoc executions cost real money (network egress, search API quota) and
    # are the one path that runs a tool with no approval row, so throttle hard.
    dependencies=[Depends(rate_limit_user(20, 60, "tool_test"))],
)
async def test_tool(
    payload: ToolTestRequest,
    user: User = Depends(get_current_user),
) -> ToolTestResult:
    return await tool_service.test_tool(payload.name, payload.arguments, user=user)
