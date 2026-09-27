"""Tool calling abstraction. All tools register with ToolRegistry; the agent loop asks
the registry for schemas and to execute calls. Business code never invokes tools ad hoc.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class ToolError(RuntimeError):
    pass


@dataclass
class ToolParameter:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = True
    default: Any = None
    enum: list[str] | None = None


class BaseTool(ABC):
    # Subclasses override these class attributes.
    name: str = ""
    description: str = ""
    category: str = "general"
    dangerous: bool = False          # requires elevated confirmation (e.g. code exec)
    # Tenant scope is mandatory for this tool: it reads resources owned by one
    # user, so :class:`~app.agents.gateway.tool_gateway.ToolGateway` refuses to
    # execute it when no authenticated principal is bound (fail closed).
    requires_user: bool = False
    # Explicitly safe for a logged-in (non-admin) user to invoke ad hoc through
    # ``POST /api/tools/test``. Default False: an unknown tool is NOT testable,
    # so newly added tools never widen the ad-hoc surface by accident. Admins may
    # test any tool that the environment permission gate also allows.
    user_testable: bool = False
    parameters: list[ToolParameter] = []

    @abstractmethod
    async def run(self, **kwargs: Any) -> Any:
        """Execute the tool. Return a JSON-serialisable result."""

    def to_openai_schema(self) -> dict[str, Any]:
        props: dict[str, Any] = {}
        required: list[str] = []
        for p in self.parameters:
            schema: dict[str, Any] = {"type": p.type, "description": p.description}
            if p.enum:
                schema["enum"] = p.enum
            if p.default is not None:
                schema["default"] = p.default
            props[p.name] = schema
            if p.required:
                required.append(p.name)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                },
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if not tool.name:
            raise ToolError("Tool must define a name")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise ToolError(f"Unknown tool: {name}")
        return self._tools[name]

    def _disabled_names(self) -> frozenset[str]:
        """运营当前关掉的那批工具名（惰性 import：本模块被几乎所有链路导入）。"""
        from app.services.tool_toggles import disabled_tools

        return disabled_tools()

    def list(self, *, include_disabled: bool = False) -> list[BaseTool]:
        """已注册的工具；**默认不含**运营已停用的那些。

        把过滤放进默认值而不是每个调用点自己写：以后新增一个列举点，"忘了考虑开关"
        的后果是少 advertise 一个工具（与执行侧一致、安全），而不是把工具摆在模型
        面前、等模型真去调用才在网关那里撞墙 —— 那一撞对用户是一次失败的回答，对
        模型是一段看不懂的报错，而这正是加这个开关想消掉的东西。

        后台目录要全量（含被关的那些 —— 否则没有第二个地方能把它重新打开），显式传
        ``include_disabled=True``。
        """
        disabled = self._disabled_names()
        if not disabled or include_disabled:
            return list(self._tools.values())
        return [tool for tool in self._tools.values() if tool.name not in disabled]

    def openai_schemas(self, only: list[str] | None = None) -> list[dict[str, Any]]:
        """交给模型的工具清单（同样按启停过滤）。

        ``only`` 通常来自 :meth:`list`，那时已经滤过一遍；这里再滤一次是因为
        ``only`` 也可能是调用点自己拼出来的名单（意图路由的 hints、planner 的
        step.allowed_tools），那条路径上没有别的共同出口。
        """
        names = only if only is not None else list(self._tools)
        disabled = self._disabled_names()
        return [
            self._tools[n].to_openai_schema()
            for n in names
            if n in self._tools and n not in disabled
        ]
