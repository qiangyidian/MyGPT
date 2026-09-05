"""Single-registry slash-command surface (Codex pattern).

Codex keeps its ~60 slash commands as ONE enum where each variant carries its
own metadata (description, inline-arg support, availability during a task,
aliases, visibility). One source of truth → parse, help text, popup ordering,
and aliases never drift; a new command is one registry entry + one handler.

This is the Python analogue: a :data:`CommandSpec` registry with parse (incl.
aliases), help, and dispatch. Ordering is registration order (explicit, not
alphabetic) so the /help popup is curated.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CommandSpec:
    name: str                                   # without leading '/', e.g. "mode"
    description: str = ""
    supports_inline_args: bool = False          # "/mode analyst"
    available_during_task: bool = False         # usable while an agent run is in flight
    aliases: tuple[str, ...] = ()
    handler: Callable[[str], Any] | None = None  # inline-arg -> result


_REGISTRY: list[CommandSpec] = []
_BY_KEY: dict[str, CommandSpec] = {}


def register(spec: CommandSpec) -> CommandSpec:
    _REGISTRY.append(spec)
    _BY_KEY[spec.name] = spec
    for a in spec.aliases:
        _BY_KEY[a] = spec
    return spec


def all_commands() -> list[CommandSpec]:
    return list(_REGISTRY)


def help_text() -> str:
    lines = ["可用命令："]
    width = max((len(c.name) for c in _REGISTRY), default=8)
    for c in _REGISTRY:
        alias = f" ({', '.join('/' + a for a in c.aliases)})" if c.aliases else ""
        lines.append(f"  /{c.name:<{width}}  {c.description}{alias}")
    return "\n".join(lines)


def parse(text: str) -> tuple[CommandSpec | None, str]:
    """Parse "/name args..." → (spec, inline_args) or (None, text)."""
    s = (text or "").strip()
    if not s.startswith("/"):
        return None, s
    body = s[1:]
    name, _, inline = body.partition(" ")
    spec = _BY_KEY.get(name.strip().lower())
    return spec, inline.strip()


def dispatch(text: str) -> Any:
    """Parse + run a command's handler; returns (spec, result) or (None, help_text())."""
    spec, inline = parse(text)
    if spec is None:
        return None, help_text()
    result = spec.handler(inline) if spec.handler else None
    return spec, result


def reset_registry() -> None:
    """Test helper — clear the registry."""
    _REGISTRY.clear()
    _BY_KEY.clear()


# ---- a few built-in commands (no side effects; handlers wired later) -------
_BUILTINS = [
    CommandSpec(name="help", description="显示可用命令", aliases=("?",)),
    CommandSpec(name="mode", description="切换模式/行为 profile", supports_inline_args=True),
    CommandSpec(name="personality", description="设置沟通风格", supports_inline_args=True),
    CommandSpec(name="skills", description="列出可用技能"),
    CommandSpec(name="compact", description="立即压缩上下文", available_during_task=False),
    CommandSpec(name="reasoning", description="设置推理强度 (none/low/medium/high/max)", supports_inline_args=True),
]
for _c in _BUILTINS:
    register(_c)
