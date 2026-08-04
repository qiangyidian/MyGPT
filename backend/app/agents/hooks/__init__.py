"""Lifecycle-hooks engine (Codex pattern): external commands that receive typed
JSON on stdin and return a typed JSON decision on stdout.

Public surface:
  * :class:`HookHandler` — one hook binding (command list OR in-process callable).
  * :class:`HookEngine` — runs handlers and folds pre-tool-use decisions.
  * :class:`HookResult` / :class:`PreToolUseResult` — decision models.
  * :class:`HookInput` + event subclasses — stdin envelopes.
  * :func:`trust_hash` / :func:`trust_status` / :class:`HookTrustStatus` —
    handler identity + trust classification.
"""
from __future__ import annotations

from app.agents.hooks.engine import (
    HookEngine,
    HookHandler,
    HookTrustStatus,
    trust_hash,
    trust_status,
)
from app.agents.hooks.schema import (
    HookInput,
    HookResult,
    PostToolUseInput,
    PreToolUseInput,
    PreToolUseResult,
    SessionStartInput,
    StopInput,
    UserPromptSubmitInput,
)

__all__ = [
    "HookEngine",
    "HookHandler",
    "HookInput",
    "HookResult",
    "HookTrustStatus",
    "PostToolUseInput",
    "PreToolUseInput",
    "PreToolUseResult",
    "SessionStartInput",
    "StopInput",
    "UserPromptSubmitInput",
    "trust_hash",
    "trust_status",
]
