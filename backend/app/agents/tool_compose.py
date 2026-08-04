"""Restricted tool-composition DSL — a safe, reduced "code mode" (Codex pattern).

Codex's ``code-mode`` runs sandboxed JS that composes many tool calls in one
shot (loop / branch / fan-out) instead of N sequential round-trips. A full V8
sandbox isn't a good fit for a Python backend, so this is the **declarative
reduction**: the model emits a JSON *program* of tool operations with simple
result interpolation (``${0.url}`` → "the ``url`` field of op 0's result"),
executed sequentially against a tool caller.

Safety properties (what makes this "sandboxed" without a VM):
  * No ``eval`` / no arbitrary code — only a fixed interpreter over declarative ops.
  * Op count is capped (``max_ops``) so a malicious program can't loop forever.
  * Interpolation is read-only path lookup against prior results — it can't call
    anything or escape.
  * The caller injects ``call_tool`` (the registry/gateway), so normal tool
    permission/approval/audit still applies per call.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

_MAX_OPS = 20

# ${N.path} or ${name.path}  (path is optional, dot-separated)
_REF = re.compile(r"\$\{([A-Za-z0-9_]+)(?:\.([A-Za-z0-9_.\[\]]+))?\}")


class ToolComposeError(ValueError):
    """Raised when the program is malformed, references an unknown result, or exceeds the op cap."""


@dataclass
class ComposeOp:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    as_: str | None = None  # optional name to reference this op's result later


@dataclass
class ComposeResult:
    results: list[Any] = field(default_factory=list)          # by op index
    named: dict[str, Any] = field(default_factory=dict)       # by op `as` name
    ops: list[ComposeOp] = field(default_factory=list)


def parse_program(program: Any) -> list[ComposeOp]:
    """Validate + parse a JSON-decoded program into ComposeOps."""
    if not isinstance(program, dict) or not isinstance(program.get("ops"), list):
        raise ToolComposeError("program must be an object with an 'ops' array")
    raw_ops = program["ops"]
    if len(raw_ops) > _MAX_OPS:
        raise ToolComposeError(f"program has {len(raw_ops)} ops; max is {_MAX_OPS}")
    ops: list[ComposeOp] = []
    for i, raw in enumerate(raw_ops):
        if not isinstance(raw, dict) or not isinstance(raw.get("tool"), str):
            raise ToolComposeError(f"op {i}: must have a string 'tool'")
        args = raw.get("args", {})
        if not isinstance(args, dict):
            raise ToolComposeError(f"op {i}: 'args' must be an object")
        ops.append(ComposeOp(tool=raw["tool"], args=args, as_=raw.get("as")))
    return ops


def execute_program(
    program: Any,
    call_tool: Callable[[str, dict[str, Any]], Any],
) -> ComposeResult:
    """Run the program: interpolate each op's args from prior results, call the tool.

    ``call_tool(tool_name, args) -> result`` is injected by the caller (e.g. the
    ToolGateway), so every tool call still goes through permission/approval/audit.
    """
    ops = parse_program(program)
    res = ComposeResult(ops=ops)
    for i, op in enumerate(ops):
        try:
            interp_args = _interpolate(op.args, res)
        except (KeyError, IndexError, ValueError) as exc:
            raise ToolComposeError(f"op {i} ({op.tool}): bad interpolation: {exc}") from exc
        try:
            result = call_tool(op.tool, interp_args)
        except Exception as exc:  # noqa: BLE001 — surface tool failure to the model
            result = {"error": str(exc)}
        res.results.append(result)
        if op.as_:
            res.named[op.as_] = result
    return res


# --------------------------------------------------------------------------- #
# Interpolation
# --------------------------------------------------------------------------- #
def _interpolate(value: Any, res: ComposeResult) -> Any:
    """Recursively replace ``${ref}`` references inside dicts/lists/strings."""
    if isinstance(value, str):
        return _interpolate_str(value, res)
    if isinstance(value, dict):
        return {k: _interpolate(v, res) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, res) for v in value]
    return value


def _interpolate_str(s: str, res: ComposeResult) -> Any:
    """If ``s`` is a single full-string reference, return the pointed-at value
    verbatim (preserves type); otherwise substring-replace within the string."""
    match_full = _REF.fullmatch(s.strip())
    if match_full:
        return _resolve(match_full.group(1), match_full.group(2), res)

    def _sub(m: re.Match[str]) -> str:
        resolved = _resolve(m.group(1), m.group(2), res)
        return str(resolved)

    return _REF.sub(_sub, s)


def _resolve(key: str, path: str | None, res: ComposeResult) -> Any:
    # Numeric key -> results[index]; else named.
    if key.isdigit():
        idx = int(key)
        if idx >= len(res.results):
            raise IndexError(f"reference ${{{key}}} has no result yet (only {len(res.results)} ops ran)")
        base: Any = res.results[idx]
    elif key in res.named:
        base = res.named[key]
    else:
        raise KeyError(f"unknown reference ${{{key}}}")
    return _walk(base, path) if path else base


def _walk(base: Any, path: str) -> Any:
    """Walk a dot/[idx] path against dicts/lists."""
    cur = base
    for part in path.split("."):
        if part == "":
            continue
        # Support [n] index segments, e.g. items[0]
        m = re.match(r"^([A-Za-z0-9_]+)(\[\d+\])?$", part)
        if not m:
            raise ValueError(f"bad path segment: {part}")
        if isinstance(cur, dict):
            if m.group(1) not in cur:
                raise KeyError(m.group(1))
            cur = cur[m.group(1)]
        elif isinstance(cur, list):
            cur = cur[int(m.group(1))]
        else:
            raise ValueError(f"cannot index into {type(cur).__name__} at {part}")
        if m.group(2):  # trailing [n]
            idx = int(m.group(2)[1:-1])
            cur = cur[idx] if isinstance(cur, list) else (_ for _ in ()).throw(ValueError("not a list"))
    return cur
