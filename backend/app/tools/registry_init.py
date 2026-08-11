"""Build the default ToolRegistry populated with builtin tools.

Other modules (the agent loop, the tools router) ask this for the registry rather
than constructing one themselves, so the set of available tools has one source of
truth.

Task 8 adds the workspace-confined tool set (:func:`get_workspace_registry`) as a
SEPARATE factory: ``get_default_registry()`` is byte-identical to its pre-Task-8
behaviour so existing callers/tests are unaffected. Workspace tools are opt-in —
a caller passes a workspace root to :func:`get_workspace_registry` (or calls
:func:`register_workspace_tools` to augment an existing registry).
"""
from __future__ import annotations

from pathlib import Path

from app.tools.base import ToolRegistry, ToolError
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


def register_workspace_tools(
    registry: ToolRegistry,
    workspace_root: str | Path,
    *,
    runner: "object | None" = None,
    output_limit: int | None = None,
) -> ToolRegistry:
    """Register the workspace-confined tools onto ``registry``.

    Each tool binds to ``workspace_root`` at construction; every path it touches
    is resolved and required to remain under that root. The shell/git tools share
    the given ``runner`` (defaults to a dev-gated LocalRunner). Returns the same
    registry for chaining.
    """
    # Late import: keeps the default registry importable without the sandbox
    # package (and its asyncio dependency) being loaded.
    from app.agents.sandbox.base import Runner
    from app.agents.sandbox.local import LocalRunner
    from app.tools.workspace import (
        WORKSPACE_TOOL_CLASSES,
        WorkspaceApplyPatchTool,
        WorkspaceGitDiffTool,
        WorkspaceGitStatusTool,
        WorkspaceShellTool,
        WorkspaceWriteTool,
    )

    # Validate the RAW input BEFORE Path() coercion: ``Path("")`` becomes
    # ``Path(".")`` (truthy), which would silently bind the workspace to the
    # process CWD — in production that is the application source tree. Reject
    # empty / whitespace-only / non-path-like roots up front.
    if not isinstance(workspace_root, (str, Path)) or not str(workspace_root).strip():
        raise ToolError("workspace_root must be a non-empty path")
    root = Path(workspace_root).resolve()
    r = runner if isinstance(runner, Runner) else LocalRunner()
    from app.core.config import get_settings

    out = output_limit if output_limit is not None else get_settings().SANDBOX_OUTPUT_LIMIT

    # Read-only / low-risk tools (no runner needed).
    for cls in WORKSPACE_TOOL_CLASSES:
        if cls in (WorkspaceShellTool, WorkspaceGitStatusTool, WorkspaceGitDiffTool):
            registry.register(cls(root, runner=r, output_limit=out))
        elif cls is WorkspaceWriteTool:
            registry.register(cls(root))
        elif cls is WorkspaceApplyPatchTool:
            registry.register(cls(root))
        else:
            registry.register(cls(root))
    return registry


def get_workspace_registry(
    workspace_root: str | Path,
    *,
    include_builtins: bool = True,
    runner: "object | None" = None,
    output_limit: int | None = None,
) -> ToolRegistry:
    """A fresh registry with the workspace tools bound to ``workspace_root``.

    By default the safe builtin tools (datetime_now, etc.) are included so a
    workspace-enabled agent has the usual utilities; pass ``include_builtins=False``
    for a workspace-only registry.
    """
    registry = get_default_registry() if include_builtins else ToolRegistry()
    return register_workspace_tools(
        registry, workspace_root, runner=runner, output_limit=output_limit
    )


__all__ = ["get_default_registry", "get_workspace_registry", "register_workspace_tools"]
