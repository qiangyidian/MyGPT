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
    # 运营启停（条目 34③）。默认 True 是给"根本没接启停的构造点"留的：一个没被登记
    # 过的工具就是可用的，让默认值落在 False 上会叫每一个既有的目录/测试都凭空多出
    # 一个"被停用"的行。
    enabled: bool = True
    # 停用/启用时留下的理由。没有它，一周后没人知道当初为什么停，"重新打开"就变成
    # 一次无人负责的赌博。
    toggle_note: str | None = None


class ToolTestRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = {}


class ToolToggleRequest(BaseModel):
    """后台启停一个工具。``note`` 可以留空，但界面会一直追问它。"""

    enabled: bool
    note: str | None = None


class ToolTestResult(BaseModel):
    ok: bool
    result: Any = None
    error: str | None = None
    latency_ms: int = 0
