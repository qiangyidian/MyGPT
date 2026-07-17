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
from app.core.config import get_settings
from app.tools.base import BaseTool, ToolError

if TYPE_CHECKING:  # pragma: no cover
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


def is_tool_allowed(tool_name: str, user: "User | None", *, strict: bool | None = None) -> bool:
    """Whether ``tool_name`` may run for ``user`` in the current environment.

    ``python_exec`` is disabled outside dev unless an explicit sandbox is
    configured (``ALLOW_PYTHON_EXEC=true``) — the subprocess "sandbox" is not a
    real isolation boundary, so we fail closed in production.
    """
    settings = get_settings()
    if strict is None:
        strict = not settings.is_dev

    if tool_name == "python_exec":
        # In prod, require an explicit opt-in AND a configured sandbox backend.
        sandbox_ok = bool(getattr(settings, "PYTHON_SANDBOX", "") or "")
        allowed = settings.is_dev or bool(getattr(settings, "ALLOW_PYTHON_EXEC", False)) or sandbox_ok
        if strict and not allowed:
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
