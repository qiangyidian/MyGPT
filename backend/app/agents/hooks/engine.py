"""Lifecycle-hooks engine (Codex pattern).

External hook commands receive a typed JSON envelope on stdin and return a typed
JSON decision on stdout. The engine spawns the command, writes the input, reads
and parses the output, and folds multiple hooks' decisions Codex-style:

  * any ``deny`` wins;
  * the last non-None ``updated_input`` wins;
  * ``additional_context`` strings are concatenated.

Two handler shapes are supported on the same :class:`HookHandler.command` field:

  * **command list** — spawned as a subprocess. Real OS isolation and a hard
    timeout (``subprocess.run`` terminates the child on expiry). Preferred for
    untrusted / user-supplied hooks.
  * **callable** — invoked in-process: ``fn(input_dict) -> dict | str |
    HookResult | None``. Zero spawn overhead and easy to test; no hard timeout
    (Python threads are not safely killable), so the caller owns hang-safety.

Failure mode is **fail-open-safe**: on timeout, non-zero exit (except exit code
2 = explicit block), or unparseable output, the hook resolves to the default
:class:`HookResult` — continue the turn, no mutation — so a buggy hook can never
silently rewrite args or swallow output. Exit code 2 is the Codex convention for
an explicit block: the hook's stderr becomes the deny ``stop_reason``.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.agents.hooks.schema import (
    HookInput,
    HookResult,
    PreToolUseInput,
    PreToolUseResult,
)

# A handler's command is either an argv list (subprocess) or an in-process
# callable taking the input dict and returning a decision (dict / JSON str /
# HookResult subclass / None).
HandlerCommand = list[str] | Callable[[dict[str, Any]], Any]


# --------------------------------------------------------------------------- #
# Handler + trust
# --------------------------------------------------------------------------- #
@dataclass
class HookHandler:
    """One hook binding.

    Attributes:
        command: argv list (subprocess) or callable. See module docstring.
        timeout_s: hard timeout for the subprocess path. Ignored for callables.
        matcher: regex tested against the tool name. ``"*"`` (and ``""`` /
            ``"**"``) is reserved as match-all. An invalid regex falls back to
            glob (``fnmatch``) matching so ``bash_*`` style patterns just work.
        event: optional event filter (e.g. ``"pre_tool_use"``). ``None`` means
            the handler fires for every event.
    """

    command: HandlerCommand
    timeout_s: float = 10.0
    matcher: str = "*"
    event: str | None = None


class HookTrustStatus(str, Enum):
    """How much to trust a handler before executing it.

      * ``managed``  — in-process callable bundled with the app (no external cmd).
      * ``trusted``  — recorded hash matches the current command identity.
      * ``modified`` — command identity changed since trust was recorded.
      * ``untrusted``— no trust record at all.
    """

    managed = "managed"
    trusted = "trusted"
    modified = "modified"
    untrusted = "untrusted"


def trust_hash(handler: HookHandler) -> str:
    """Stable sha256 of the handler's (command + matcher + timeout) identity.

    Callable handlers hash on ``(module, qualname)`` so the same function
    identity is stable across runs (a renamed or rewritten function changes the
    digest — exactly what :func:`trust_status` should detect).
    """
    if callable(handler.command):
        cmd_repr = [
            getattr(handler.command, "__module__", ""),
            getattr(handler.command, "__qualname__", ""),
        ]
    else:
        cmd_repr = [str(c) for c in handler.command]
    payload = json.dumps(
        {"command": cmd_repr, "matcher": handler.matcher, "timeout_s": handler.timeout_s},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def trust_status(handler: HookHandler, trusted_hash: str | None) -> HookTrustStatus:
    """Classify a handler's trust given a previously-recorded trusted hash."""
    if callable(handler.command):
        return HookTrustStatus.managed
    if not trusted_hash:
        return HookTrustStatus.untrusted
    return HookTrustStatus.trusted if trust_hash(handler) == trusted_hash else HookTrustStatus.modified


# --------------------------------------------------------------------------- #
# Matcher
# --------------------------------------------------------------------------- #
def _matcher_hits(matcher: str, tool_name: str) -> bool:
    """True if ``matcher`` matches ``tool_name``.

    ``"*"`` / ``""`` / ``"**"`` match everything. Otherwise the matcher is tried
    as a full-match regex; an invalid pattern falls back to ``fnmatch`` globs.
    """
    m = (matcher or "*").strip()
    if m in ("*", "**"):
        return True
    name = tool_name or ""
    try:
        return re.fullmatch(m, name) is not None
    except re.error:
        return fnmatch.fnmatchcase(name, m)


# --------------------------------------------------------------------------- #
# Output coercion
# --------------------------------------------------------------------------- #
def _coerce(raw: Any, result_cls: type[HookResult]) -> HookResult:
    """Coerce a hook's raw return value into ``result_cls``.

    Accepts ``dict`` / JSON ``str`` / :class:`HookResult` / ``None``. Unknown
    keys are ignored (``extra="ignore"``); an invalid value for any field (e.g.
    a bad ``permission_decision`` literal) drops the whole result to the safe
    default rather than partially applying it.
    """
    if raw is None:
        return result_cls()
    if isinstance(raw, HookResult):
        if isinstance(raw, result_cls):
            return raw
        try:
            return result_cls.model_validate(raw.model_dump(by_alias=True))
        except Exception:
            return result_cls()
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return result_cls()
        try:
            raw = json.loads(text)
        except (ValueError, TypeError):
            return result_cls()
    if not isinstance(raw, dict):
        return result_cls()
    try:
        return result_cls.model_validate(raw)
    except Exception:
        return result_cls()


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class HookEngine:
    """Runs registered hooks and folds their decisions."""

    def __init__(self, handlers: list[HookHandler] | None = None) -> None:
        self._handlers: list[HookHandler] = list(handlers or [])

    def register(self, handler: HookHandler) -> None:
        """Append a handler to the registry."""
        self._handlers.append(handler)

    # -- selection ----------------------------------------------------------
    def select(self, event_name: str, tool_name: str = "") -> list[HookHandler]:
        """Registry handlers whose ``event`` (if set) == ``event_name`` and whose
        ``matcher`` hits ``tool_name``."""
        return [
            h for h in self._handlers
            if (h.event is None or h.event == event_name)
            and _matcher_hits(h.matcher, tool_name)
        ]

    # -- single invocation --------------------------------------------------
    def run_one(
        self,
        handler: HookHandler,
        input_model: HookInput,
        result_cls: type[HookResult] = HookResult,
    ) -> HookResult:
        """Invoke one handler and parse its decision into ``result_cls``.

        Any failure (timeout, non-zero/non-2 exit, unparseable output, raised
        exception) resolves to the ``result_cls`` safe default.
        """
        payload = input_model.model_dump()
        cmd = handler.command

        if callable(cmd):
            try:
                raw = cmd(payload)
            except Exception:
                return result_cls()
            return _coerce(raw, result_cls)

        # command (subprocess) path
        try:
            proc = subprocess.run(
                [str(c) for c in cmd],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=handler.timeout_s,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return result_cls()

        if proc.returncode == 2:
            # Codex convention: exit code 2 == explicit block; stderr is the reason.
            reason = (proc.stderr or proc.stdout or "").strip() or "blocked by hook (exit 2)"
            try:
                return result_cls.model_validate(
                    {"continue": False, "stop_reason": reason, "suppress_output": True}
                )
            except Exception:
                return result_cls()
        if proc.returncode != 0:
            return result_cls()
        return _coerce(proc.stdout, result_cls)

    # -- pre-tool-use fold --------------------------------------------------
    def run_pre_tool_use(
        self,
        tool_name: str,
        tool_input: dict,
        handlers: list[HookHandler] | None = None,
        *,
        session_id: str = "",
        turn_id: str = "",
        cwd: str = "",
    ) -> PreToolUseResult:
        """Fold a set of pre-tool-use hooks Codex-style.

          * any ``deny`` (``permission_decision == "deny"`` OR ``continue_`` is
            False, the latter covering exit-code-2 blocks) wins;
          * the last non-None ``updated_input`` wins;
          * ``additional_context`` strings are concatenated (blank-line joined).

        ``handlers`` defaults to :meth:`select` over the registry. When passed
        explicitly it is still filtered by matcher so callers can hand the full
        registry list in safely.
        """
        src = handlers if handlers is not None else self.select("pre_tool_use", tool_name)
        matching = [h for h in src if _matcher_hits(h.matcher, tool_name)]
        inp = PreToolUseInput(
            session_id=session_id,
            turn_id=turn_id,
            cwd=cwd,
            tool_name=tool_name,
            tool_input=dict(tool_input or {}),
        )

        denied = False
        deny_reason = ""
        saw_allow = False
        updated_input: dict | None = None
        contexts: list[str] = []
        system_messages: list[str] = []
        suppress = False

        for h in matching:
            r = self.run_one(h, inp, PreToolUseResult)
            if r.permission_decision == "deny" or not r.continue_:
                denied = True
                if r.stop_reason and not deny_reason:
                    deny_reason = r.stop_reason
            elif r.permission_decision == "allow":
                saw_allow = True
            # updated_input / additional_context are collected even when another
            # hook denied — but a deny short-circuits the returned decision below.
            if r.updated_input is not None:
                updated_input = r.updated_input
            if r.additional_context:
                contexts.append(r.additional_context)
            if r.system_message:
                system_messages.append(r.system_message)
            suppress = suppress or r.suppress_output

        if denied:
            return PreToolUseResult.model_validate(
                {
                    "continue": False,
                    "permission_decision": "deny",
                    "stop_reason": deny_reason or "denied by hook",
                    "suppress_output": suppress,
                }
            )

        return PreToolUseResult.model_validate(
            {
                "continue": True,
                "permission_decision": "allow" if saw_allow else None,
                "updated_input": updated_input,
                "additional_context": "\n\n".join(contexts) if contexts else None,
                "system_message": "\n\n".join(system_messages) if system_messages else "",
                "suppress_output": suppress,
            }
        )
