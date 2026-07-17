"""Build the default ToolRegistry populated with builtin tools.

Other modules (the agent loop, the tools router) ask this for the registry rather
than constructing one themselves, so the set of available tools has one source of
truth.
"""
from __future__ import annotations

from app.tools.base import ToolRegistry
from app.tools.builtin import (
    DateTimeNowTool,
    DbQueryTool,
    FileAnalyzeTool,
    HttpGetTool,
    PythonExecTool,
    WebSearchTool,
)


def get_default_registry() -> ToolRegistry:
    """Return a fresh registry with all builtin tools registered."""
    registry = ToolRegistry()
    for tool_cls in (
        DateTimeNowTool,
        HttpGetTool,
        WebSearchTool,
        PythonExecTool,
        DbQueryTool,
        FileAnalyzeTool,
    ):
        registry.register(tool_cls())
    return registry


__all__ = ["get_default_registry"]
