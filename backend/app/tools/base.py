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

    def list(self) -> list[BaseTool]:
        return list(self._tools.values())

    def openai_schemas(self, only: list[str] | None = None) -> list[dict[str, Any]]:
        names = only if only is not None else list(self._tools)
        return [self._tools[n].to_openai_schema() for n in names if n in self._tools]
