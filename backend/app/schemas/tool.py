from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ToolParameter(BaseModel):
    name: str
    type: str
    description: str
    required: bool = True
    default: Any = None
    enum: list[str] | None = None


class ToolInfo(BaseModel):
    name: str
    description: str
    parameters: list[ToolParameter]
    category: str = "general"
    dangerous: bool = False        # e.g. code execution


class ToolTestRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = {}


class ToolTestResult(BaseModel):
    ok: bool
    result: Any = None
    error: str | None = None
    latency_ms: int = 0
