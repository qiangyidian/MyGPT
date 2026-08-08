"""Tools router: list available tools + ad-hoc test execution.

Both go through the default registry, which is also what the chat agent loop uses.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.deps import get_current_user
from app.models import User
from app.schemas import ToolInfo, ToolTestRequest, ToolTestResult
from app.services import tool_service

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("", response_model=list[ToolInfo])
async def list_tools(
    user: User = Depends(get_current_user),
) -> list[ToolInfo]:
    return tool_service.list_tools()


@router.post("/test", response_model=ToolTestResult)
async def test_tool(
    payload: ToolTestRequest,
    user: User = Depends(get_current_user),
) -> ToolTestResult:
    return await tool_service.test_tool(payload.name, payload.arguments, user=user)
