"""Tool safety policy: risk classification, allow-listing, and SQL hardening.

This is the *policy* layer; :class:`~app.agents.gateway.tool_gateway.ToolGateway`
enforces it. The two dangerous builtin tools (``python_exec``, ``db_query``)
must not execute just because the model asked — they need approval, and the SQL
guard must be stronger than a naive ``startswith("select")``.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.agents.schemas import RiskLevel
from app.core.config import SANDBOX_MODE_DOCKER, env_flag, get_settings
from app.tools.base import BaseTool, ToolError

if TYPE_CHECKING:  # pragma: no cover
    from app.core.config import Settings
    from app.models import User

# Tools that always require human approval before running.
_DANGEROUS_TOOL_RISK: dict[str, RiskLevel] = {
    "python_exec": RiskLevel.high,
    "db_query": RiskLevel.high,
}
# Network tools are medium risk (SSRF / cost), not auto-approved in strict mode.
_NETWORK_TOOL_RISK: dict[str, RiskLevel] = {
    "http_get": RiskLevel.medium,
    "web_search": RiskLevel.low,
    "file_analyze": RiskLevel.low,
    "datetime_now": RiskLevel.low,
}


def risk_level_for(tool: BaseTool) -> RiskLevel:
    """Classify a tool's risk. Dangerous flag wins; otherwise name-based."""
    if tool.dangerous:
        return _DANGEROUS_TOOL_RISK.get(tool.name, RiskLevel.high)
    return _NETWORK_TOOL_RISK.get(tool.name, RiskLevel.low)


def should_require_approval(tool: BaseTool) -> bool:
    """True when this tool must be gated behind a human approval."""
    return tool.dangerous


def _has_admin_role(user: User | None) -> bool:
    """True when ``user`` carries the admin role (None-safe)."""
    return bool(user is not None and getattr(user, "role", "") == "admin")


def isolated_sandbox_configured(settings: Settings) -> bool:
    """是否真的配好了一个「隔离执行后端」（而不是有人随手写了个 ``PYTHON_SANDBOX=0``）。

    两个条件同时成立才算数，缺一不算：

      1. ``PYTHON_SANDBOX`` 解析后 == ``docker`` —— 本仓库唯一真实存在的后端。
         ``e2b`` / ``gvisor`` 只是配置注释里的占位名，写了也不放行；
         ``"0"`` / ``"false"`` / ``"no"`` 这类「非空但明显是关」的取值更不是。
      2. ``SANDBOX_MODE`` == ``docker`` —— 即 runner 工厂真的会给出
         :class:`~app.agents.sandbox.docker.DockerRunner`。后端名字写对了但
         runner 还停在 local（子进程带本进程权限），所谓隔离就是句空话。

    ``SANDBOX_MODE`` 取值非法时（工厂会抛）按「未配置」处理：安全判断里任何
    不确定都等于拒绝，绝不让一次配置笔误变成放行。
    """
    backend = str(getattr(settings, "PYTHON_SANDBOX", "") or "").strip().lower()
    if backend != SANDBOX_MODE_DOCKER:
        return False
    from app.agents.sandbox.factory import SandboxConfigError, sandbox_mode

    try:
        return sandbox_mode(settings) == SANDBOX_MODE_DOCKER
    except SandboxConfigError:
        return False


def python_exec_enabled(settings: Settings) -> bool:
    """``python_exec`` 在当前配置下是否可用（唯一判定口径）。

    生产里的语义是 **AND**：``ALLOW_PYTHON_EXEC``（按 :func:`env_flag` 解析，
    ``"0"``/``"false"``/``""`` 都是关）**且** 真隔离后端已配好
    （:func:`isolated_sandbox_configured`）。这里曾经写成 ``or``，于是
    ``PYTHON_SANDBOX`` 被填成任意非空字符串就能在生产放行任意代码执行。
    dev 仍然直接可用（本地开发路径，执行仍受 runner 的限额/环境拒绝约束）。
    """
    if settings.is_dev:
        return True
    return env_flag(getattr(settings, "ALLOW_PYTHON_EXEC", False)) and isolated_sandbox_configured(
        settings
    )


def is_tool_allowed(tool_name: str, user: User | None, *, strict: bool | None = None) -> bool:
    """Whether ``tool_name`` may run for ``user`` in the current environment.

    ``python_exec`` is disabled outside dev unless an explicit opt-in combines
    with a real isolation backend (see :func:`python_exec_enabled`) — the
    subprocess "sandbox" is not a real isolation boundary, so we fail closed in
    production.

    ``db_query`` reads the WHOLE application database — every tenant's rows
    (users, conversations, messages). For a regular C-end user it is a
    cross-tenant read primitive whether reached via /api/tools/test or via
    their own agent's approval flow, so in production it is admin-only.
    Dev/test keeps it open for development and the test-suite.

    Operator kill-switch first and absolute: a tool the admin disabled in
    /admin (``tool_toggles``) is refused here no matter who asks, how it is
    routed, or whether an approval row exists. This is the execution gate the
    advertise-side filter (``ToolRegistry.list``) mirrors — the mirror is UX,
    this is the guarantee.
    """
    from app.services.tool_toggles import is_disabled

    if is_disabled(tool_name):
        return False

    settings = get_settings()
    if strict is None:
        strict = not settings.is_dev

    if tool_name == "python_exec":
        if strict and not python_exec_enabled(settings):
            return False
    if tool_name == "db_query":
        if strict and not _has_admin_role(user):
            return False
    return True


# --------------------------------------------------------------------------- #
# SQL hardening (replaces DbQueryTool's startswith("select") guard)
# --------------------------------------------------------------------------- #
# Keywords that have no business in a read-only query. Word-boundary matched.
# Kept tight on purpose: common-as-identifier words (set, comment, replace) are
# omitted to avoid false positives; the must-start-with-SELECT/WITH + no-`;`
# rules already block standalone control/DML statements.
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b("
    r"insert|update|delete|drop|alter|truncate|merge|grant|revoke|create|"
    r"vacuum|copy|attach|detach|pragma|call|exec|execute|"
    r"into|"  # blocks Postgres SELECT ... INTO (writes a table)
    r"pg_sleep|pg_terminate_backend|lo_export|lo_import|pg_read_file|pg_ls_dir"
    r")\b",
    re.IGNORECASE,
)

# Strip SQL line/block comments before analysis so a keyword can't hide in one.
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


class UnsafeSQLError(ToolError):
    """Raised when a SQL statement is not a safe read-only SELECT."""


def validate_readonly_sql(sql: str) -> str:
    """Return a sanitized read-only SELECT, or raise :class:`UnsafeSQLError`.

    Rules (defense in depth, no full parser dependency):
      * non-empty, single statement (no ``;`` separating statements)
      * first meaningful token is ``SELECT`` or ``WITH`` (CTE)
      * no DML/DDL/session/control keywords anywhere
      * no ``;`` (a trailing one is tolerated and stripped)
    """
    if not sql or not sql.strip():
        raise UnsafeSQLError("empty SQL")

    cleaned = _BLOCK_COMMENT.sub(" ", sql)
    cleaned = _LINE_COMMENT.sub(" ", cleaned)
    stripped = cleaned.strip().rstrip(";").strip()

    if not stripped:
        raise UnsafeSQLError("empty SQL after stripping comments")

    # Reject multiple statements.
    if ";" in stripped:
        raise UnsafeSQLError("multiple statements are not allowed")

    # Must start with SELECT or WITH.
    first = re.match(r"^([A-Za-z_]+)", stripped)
    first_word = (first.group(1).lower() if first else "")
    if first_word not in {"select", "with"}:
        raise UnsafeSQLError(
            f"only read-only SELECT/WITH statements are allowed (got {first_word!r})"
        )

    # No forbidden keywords anywhere in the body.
    bad = _FORBIDDEN_KEYWORDS.search(stripped)
    if bad:
        raise UnsafeSQLError(f"forbidden keyword in SQL: {bad.group(1)!r}")

    return stripped
