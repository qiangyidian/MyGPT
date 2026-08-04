"""Pydantic schemas for the lifecycle-hooks engine.

A hook is an external command (or in-process callable) that receives a typed
:class:`HookInput` as JSON on stdin and replies with a typed :class:`HookResult`
decision on stdout. :class:`PreToolUseResult` extends the universal result with
the three pre-tool levers Codex hooks expose: allow/deny, argument rewrite, and
extra-context injection.

Field-naming note: ``continue`` is a Python keyword, so the model field is
``continue_`` aliased to ``"continue"`` — the JSON wire key stays ``continue``
(hooks read/write ``{"continue": false}``); Python code uses ``result.continue_``.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- #
# Results (hook stdout -> Python)
# --------------------------------------------------------------------------- #
class HookResult(BaseModel):
    """Universal hook decision. Defaults are FAIL-OPEN-SAFE: continue the turn,
    no mutation, no suppressed output, no injected system message.

    A hook that times out, exits non-zero (except exit code 2), or emits
    unparseable JSON resolves to exactly these defaults.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    continue_: bool = Field(default=True, alias="continue")
    stop_reason: str = ""
    suppress_output: bool = False
    system_message: str = ""


class PreToolUseResult(HookResult):
    """Pre-tool-use decision carrying the three Codex levers.

      * ``permission_decision`` — ``"allow"`` / ``"deny"`` / ``None`` (passthrough).
      * ``updated_input`` — a rewritten tool-input dict (replaces the original).
      * ``additional_context`` — text injected into the model's context.
    """

    permission_decision: Literal["allow", "deny"] | None = None
    updated_input: dict | None = None
    additional_context: str | None = None


# --------------------------------------------------------------------------- #
# Inputs (Python -> hook stdin)
# --------------------------------------------------------------------------- #
class HookInput(BaseModel):
    """Base envelope serialized to JSON and handed to the hook on stdin.

    Kept free of ORM/DB objects so serialization is trivial and the hook process
    never needs the app's imports. Subclasses fix ``event`` and add the few
    event-specific fields that matter.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    event: str = ""
    session_id: str = ""
    turn_id: str = ""
    cwd: str = ""
    tool_name: str = ""
    tool_input: dict = Field(default_factory=dict)


class PreToolUseInput(HookInput):
    """Fired immediately before a tool call. ``tool_name``/``tool_input`` carry
    the call about to happen; a hook may deny it or rewrite the args."""

    event: str = "pre_tool_use"


class PostToolUseInput(HookInput):
    """Fired immediately after a tool call returns. ``tool_output`` is the
    structured result; ``tool_error`` is set when the tool raised."""

    event: str = "post_tool_use"
    tool_output: dict | None = None
    tool_error: str = ""


class UserPromptSubmitInput(HookInput):
    """Fired when the user submits a prompt. A hook may append context but the
    prompt itself is not rewriteable here (use pre-tool-use for arg rewrites)."""

    event: str = "user_prompt_submit"
    prompt: str = ""


class SessionStartInput(HookInput):
    """Fired at session start/resume/clear."""

    event: str = "session_start"
    source: str = "startup"  # startup | resume | clear


class StopInput(HookInput):
    """Fired when the agent's turn stops."""

    event: str = "stop"
    stop_reason: str = ""
