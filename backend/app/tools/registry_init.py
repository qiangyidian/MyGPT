"""Build the default ToolRegistry populated with builtin tools.

Other modules (the agent loop, the tools router) ask this for the registry rather
than constructing one themselves, so the set of available tools has one source of
truth.

Task 8 adds the workspace-confined tool set (:func:`get_workspace_registry`) as a
SEPARATE factory: ``get_default_registry()`` is byte-identical to its pre-Task-8
behaviour so existing callers/tests are unaffected **as long as**
``WORKSPACE_TOOLS_ENABLED`` is off.

Sandbox wiring (this module used to be the bug): the workspace shell/git tools
built their own ``LocalRunner()`` when no runner was passed, and LocalRunner
hard-refuses to exec outside dev/test — so a production deployment that turned on
workspace tools got a silently dead feature instead of an isolated runner. Now:

  * the runner comes from :func:`app.agents.sandbox.factory.get_sandbox_runner`
    (single construction point, keyed on ``SANDBOX_MODE``); nothing here builds a
    ``LocalRunner`` anymore;
  * which tools get registered at all is decided by the permission profile
    (:mod:`app.agents.permission_profiles`): an operation whose capability the
    profile does not grant is registered as a
    :class:`~app.tools.workspace.DeniedWorkspaceTool` that only refuses;
  * ``WORKSPACE_TOOLS_ENABLED`` + ``WORKSPACE_ROOT`` are honoured by
    :func:`get_default_registry`, which is the registry every production caller
    uses (the gateway, ``tool_service``, the runtimes). Before this, no production
    code path ever registered the workspace tools at all.
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.core.config import env_flag, get_settings
from app.tools.base import ToolError, ToolRegistry
from app.tools.builtin import (
    DateTimeNowTool,
    DbQueryTool,
    FileAnalyzeTool,
    HttpGetTool,
    PythonExecTool,
    WebSearchTool,
)

logger = logging.getLogger(__name__)


def get_default_registry() -> ToolRegistry:
    """Return a fresh registry with all builtin tools registered.

    Also the single place the workspace tool set enters the production path — see
    :func:`_register_workspace_tools_if_enabled`, which registers nothing unless
    the operator turned the flag on AND configured a root.
    """
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
    _register_workspace_tools_if_enabled(registry)
    return registry


def _register_workspace_tools_if_enabled(registry: ToolRegistry) -> None:
    """Honour ``WORKSPACE_TOOLS_ENABLED`` / ``WORKSPACE_ROOT`` (fail closed).

    A misconfigured workspace (no root, an illegal permission profile, an unknown
    ``SANDBOX_MODE``) registers NOTHING executable and logs why, rather than
    falling back to a looser setup: the safe builtins keep working, and
    ``GET /ready``'s ``runner`` check reports the same config error to the
    operator. An explicit :func:`get_workspace_registry` call, by contrast, raises
    — that caller asked for exactly this workspace and must not be lied to.
    """
    s = get_settings()
    if not env_flag(getattr(s, "WORKSPACE_TOOLS_ENABLED", False)):
        return
    root = str(getattr(s, "WORKSPACE_ROOT", "") or "").strip()
    if not root:
        logger.error(
            "WORKSPACE_TOOLS_ENABLED=true 但 WORKSPACE_ROOT 为空 —— 不注册工作区工具"
            "（fail closed：要么给出被约束的根目录，要么关掉开关）"
        )
        return
    # Late import: the sandbox package is only needed once the flag is on.
    from app.agents.sandbox.factory import SandboxConfigError

    try:
        register_workspace_tools(registry, root, settings=s)
    except (SandboxConfigError, ToolError) as exc:
        # 配置错误值得一条带栈的日志：它意味着运维以为工作区工具是开着的。
        logger.exception("工作区工具注册被跳过（配置非法，按 fail closed 不注册）: %s", exc)


def register_workspace_tools(
    registry: ToolRegistry,
    workspace_root: str | Path,
    *,
    runner: object | None = None,
    output_limit: int | None = None,
    settings: object | None = None,
) -> ToolRegistry:
    """Register the workspace-confined tools onto ``registry``.

    Each tool binds to ``workspace_root`` at construction; every path it touches
    is resolved and required to remain under that root. The shell/git tools share
    ``runner`` when the caller injects one (tests, an embedded agent); otherwise
    they resolve the sandbox factory's runner at exec time, so a registry built
    with a broken sandbox config fails loudly on the call instead of pretending
    a dev-only runner exists.

    Every tool is additionally gated by the permission profile's capabilities
    (``:read-only`` denies writes and shell; ``:workspace-write`` grants them).
    A denied operation is registered as a refusal stub so the model gets a
    reason. Returns the same registry for chaining.
    """
    # Late import: keeps the default registry importable without the sandbox /
    # policy package (and its asyncio dependency) being loaded.
    from app.agents.permission_profiles import capability_granted, capability_required_for
    from app.agents.sandbox.base import Runner
    from app.agents.sandbox.factory import permission_policy, sandbox_mode
    from app.tools.workspace import (
        WORKSPACE_TOOL_CLASSES,
        DeniedWorkspaceTool,
        WorkspaceGitDiffTool,
        WorkspaceGitStatusTool,
        WorkspaceShellTool,
    )

    # Validate the RAW input BEFORE Path() coercion: ``Path("")`` becomes
    # ``Path(".")`` (truthy), which would silently bind the workspace to the
    # process CWD — in production that is the application source tree. Reject
    # empty / whitespace-only / non-path-like roots up front.
    if not isinstance(workspace_root, (str, Path)) or not str(workspace_root).strip():
        raise ToolError("workspace_root must be a non-empty path")
    root = Path(workspace_root).resolve()

    # An injected runner must actually satisfy the Runner protocol. Silently
    # replacing a bad runner with a dev-only LocalRunner is exactly how the
    # sandbox stopped being wired in the first place.
    if runner is not None and not isinstance(runner, Runner):
        raise ToolError("runner must satisfy the app.agents.sandbox.base.Runner protocol")

    s = settings if settings is not None else get_settings()
    profile_name = str(getattr(s, "WORKSPACE_PERMISSION_PROFILE", "") or "").strip()
    # 档案/模式解析失败会抛 SandboxConfigError —— 调用方按 fail closed 处理。
    policy = permission_policy(s)
    sandbox_mode(s)  # 非法 SANDBOX_MODE 在这里就该响，而不是等到第一次执行

    if output_limit is None:
        output_limit = int(getattr(s, "SANDBOX_OUTPUT_LIMIT", 8192))

    for cls in WORKSPACE_TOOL_CLASSES:
        op = str(cls.name)
        capability = capability_required_for(op)
        if not capability_granted(policy, capability):
            registry.register(
                DeniedWorkspaceTool(op, capability=capability, profile_name=profile_name)
            )
            continue
        if cls in (WorkspaceShellTool, WorkspaceGitStatusTool, WorkspaceGitDiffTool):
            registry.register(cls(root, runner=runner, output_limit=output_limit))
        else:
            registry.register(cls(root))
    return registry


def get_workspace_registry(
    workspace_root: str | Path,
    *,
    include_builtins: bool = True,
    runner: object | None = None,
    output_limit: int | None = None,
) -> ToolRegistry:
    """A fresh registry with the workspace tools bound to ``workspace_root``.

    By default the safe builtin tools (datetime_now, etc.) are included so a
    workspace-enabled agent has the usual utilities; pass ``include_builtins=False``
    for a workspace-only registry. The explicit ``workspace_root`` always wins
    over ``WORKSPACE_ROOT`` (which :func:`get_default_registry` may already have
    registered tools against).
    """
    registry = get_default_registry() if include_builtins else ToolRegistry()
    return register_workspace_tools(
        registry, workspace_root, runner=runner, output_limit=output_limit
    )


__all__ = ["get_default_registry", "get_workspace_registry", "register_workspace_tools"]
