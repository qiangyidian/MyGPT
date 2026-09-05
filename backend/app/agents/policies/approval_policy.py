"""Approval policy: hashing, summaries, TTL.

The gateway uses these to (a) match a dangerous tool call against a prior
approval by exact arguments, (b) show the user a human-readable summary of what
they are approving, and (c) expire stale approvals.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, UTC

from app.agents.schemas import RiskLevel

# How long a pending approval stays valid before auto-expiring.
APPROVAL_TTL = timedelta(minutes=15)


def _canonical_json(arguments: dict) -> str:
    """Deterministic JSON so identical args hash identically regardless of key order."""
    return json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)


def arguments_hash(tool_name: str, arguments: dict) -> str:
    """Stable SHA-256 of (tool_name, canonical arguments)."""
    payload = f"{tool_name}|{_canonical_json(arguments)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def risk_summary(tool_name: str, arguments: dict) -> str:
    """One-line, human-readable description of what the tool will do."""
    a = arguments or {}
    if tool_name == "db_query":
        sql = str(a.get("sql", "")).strip().replace("\n", " ")
        if len(sql) > 80:
            sql = sql[:80] + "…"
        return f"读取数据库执行 SQL: {sql or '(空)'}"
    if tool_name == "python_exec":
        code = str(a.get("code", "")).strip().replace("\n", " ")
        if len(code) > 80:
            code = code[:80] + "…"
        return f"执行 Python 代码: {code or '(空)'}"
    if tool_name == "http_get":
        return f"请求网络地址: {a.get('url', '')}"
    if tool_name == "web_search":
        return f"网络搜索: {a.get('query', '')}"
    if tool_name == "file_analyze":
        return f"分析文件: {a.get('document_id', '')}"
    return f"调用工具 {tool_name}"


def expiry_from_now(ttl: timedelta | None = None) -> datetime:
    return datetime.now(UTC) + (ttl or APPROVAL_TTL)


def is_expired(expires_at: datetime | None, *, now: datetime | None = None) -> bool:
    if expires_at is None:
        return False
    now = now or datetime.now(UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return now >= expires_at


def preview(arguments: dict, *, max_chars: int = 600) -> dict:
    """Return a redacted, size-bounded copy of arguments for SSE/API display."""
    out: dict = {}
    for k, v in (arguments or {}).items():
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
        if len(s) > max_chars:
            s = s[:max_chars] + "…"
        out[k] = s
    return out


def risk_from_level(level: str | RiskLevel) -> RiskLevel:
    if isinstance(level, RiskLevel):
        return level
    try:
        return RiskLevel(level)
    except ValueError:
        return RiskLevel.medium
