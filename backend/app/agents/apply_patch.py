"""Structured file-edit primitive (Codex ``apply-patch`` pattern).

Replaces brittle ``cat > file`` / ``sed`` edits with a purpose-built, diffable,
atomic-per-file edit format the model emits as one tool call:

    *** Begin Patch
    *** Add File: hello.txt
    +Hello world
    *** Update File: src/app.py
    @@ def greet():
    -print("Hi")
    +print("Hello, world!")
    *** Delete File: obsolete.txt
    *** End Patch

This module is pure (operates on an in-memory ``{path: list[str] lines}`` map) so
it's fully unit-testable without touching the real filesystem; a thin wrapper can
read/write real files around :func:`apply_ops`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Action = Literal["add", "delete", "update"]


@dataclass
class Hunk:
    """A contiguous run of context/removed/added lines within an update."""

    lines: list[tuple[str, str]] = field(default_factory=list)  # (kind, text); kind in {"ctx","-","+"}


@dataclass
class FileOp:
    action: Action
    path: str
    move_to: str | None = None
    content: list[str] = field(default_factory=list)  # add: full file body
    hunks: list[Hunk] = field(default_factory=list)    # update: edits


@dataclass
class ApplyDelta:
    """What actually changed (for audit / rollback)."""

    path: str
    action: Action
    applied: bool
    note: str = ""


class PatchError(ValueError):
    """Raised when the patch grammar is invalid or a hunk won't apply."""


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def parse_patch(text: str) -> list[FileOp]:
    """Parse a ``*** Begin Patch`` … ``*** End Patch`` block into FileOps."""
    lines = (text or "").splitlines()
    ops: list[FileOp] = []
    i = 0
    n = len(lines)
    # Find the Begin marker (be lenient about leading whitespace/prose).
    while i < n and not lines[i].strip().startswith("*** Begin Patch"):
        i += 1
    i += 1  # skip the Begin marker
    current: FileOp | None = None
    hunk: Hunk | None = None

    def flush_hunk() -> None:
        nonlocal hunk
        if current is not None and hunk is not None and hunk.lines:
            current.hunks.append(hunk)
        hunk = None

    while i < n:
        raw = lines[i]
        s = raw.strip()
        if s.startswith("*** End Patch"):
            break
        if s.startswith("*** Add File:"):
            flush_hunk()
            current = FileOp(action="add", path=s[len("*** Add File:"):].strip())
            ops.append(current)
        elif s.startswith("*** Delete File:"):
            flush_hunk()
            current = FileOp(action="delete", path=s[len("*** Delete File:"):].strip())
            ops.append(current)
        elif s.startswith("*** Update File:"):
            flush_hunk()
            current = FileOp(action="update", path=s[len("*** Update File:"):].strip())
            ops.append(current)
        elif s.startswith("*** Move to:") and current is not None:
            current.move_to = s[len("*** Move to:"):].strip()
        elif s.startswith("*** End of File"):
            i += 1
            continue
        elif s.startswith("***"):
            # Unknown section header — be lenient, treat as a new boundary.
            flush_hunk()
            current = None
        else:
            # Content line within the current op.
            if current is None:
                i += 1
                continue
            if current.action == "add":
                # Add-file bodies are '+' lines (strip a single leading '+').
                current.content.append(raw[1:] if raw.startswith("+") else raw)
            else:  # update — classify by leading marker
                if hunk is None:
                    hunk = Hunk()
                if raw.startswith("+"):
                    hunk.lines.append(("+", raw[1:]))
                elif raw.startswith("-"):
                    hunk.lines.append(("-", raw[1:]))
                elif raw.startswith("@@"):
                    hunk.lines.append(("ctx", raw[2:].lstrip()))
                else:
                    hunk.lines.append(("ctx", raw))
        i += 1

    flush_hunk()
    return ops


# --------------------------------------------------------------------------- #
# Applier (in-memory)
# --------------------------------------------------------------------------- #
def apply_ops(ops: list[FileOp], files: dict[str, list[str]]) -> list[ApplyDelta]:
    """Apply parsed ops to an in-memory ``{path: list[line]}`` map (mutating).

    Update hunks match the removed+context lines exactly within the file and
    splice in the added lines. Raises :class:`PatchError` on a missing file or an
    un-locatable hunk (so the caller can report precisely what failed).
    """
    deltas: list[ApplyDelta] = []
    for op in ops:
        if op.action == "add":
            files[op.path] = list(op.content)
            deltas.append(ApplyDelta(op.path, "add", True))
        elif op.action == "delete":
            if op.path not in files:
                raise PatchError(f"delete: file not found: {op.path}")
            del files[op.path]
            deltas.append(ApplyDelta(op.path, "delete", True))
        else:  # update
            if op.path not in files:
                raise PatchError(f"update: file not found: {op.path}")
            src = files[op.path]
            for hunk in op.hunks:
                src = _apply_hunk(src, hunk)
            files[op.move_to or op.path] = src
            if op.move_to and op.move_to != op.path:
                files.pop(op.path, None)
            deltas.append(ApplyDelta(op.move_to or op.path, "update", True))
    return deltas


def _apply_hunk(lines: list[str], hunk: Hunk) -> list[str]:
    """Locate the hunk's context+removed block in ``lines`` and splice the added lines."""
    needle = [txt for kind, txt in hunk.lines if kind in ("ctx", "-")]
    replacement = [txt for kind, txt in hunk.lines if kind in ("ctx", "+")]
    if not needle:
        # Pure addition with no anchor — append at end.
        return lines + [txt for kind, txt in hunk.lines if kind == "+"]
    start = _find_subseq(lines, needle)
    if start == -1:
        raise PatchError(
            f"hunk context not found: {needle[:3]}…"
        )
    end = start + len(needle)
    return lines[:start] + replacement + lines[end:]


def _find_subseq(haystack: list[str], needle: list[str]) -> int:
    """First index where ``needle`` appears as a contiguous sublist (exact, stripped compare)."""
    if not needle:
        return -1
    stripped = [x.strip() for x in needle]
    for i in range(len(haystack) - len(stripped) + 1):
        if [haystack[i + j].strip() for j in range(len(stripped))] == stripped:
            return i
    return -1
