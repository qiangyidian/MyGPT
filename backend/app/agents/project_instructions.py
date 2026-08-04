"""Hierarchical AGENTS.md project-instruction loader (Codex pattern).

Codex walks the directory tree from the project root *down* to the current
working directory, concatenating every ``AGENTS.md`` it finds along the way so
that more specific (deeper) instructions can refine more general (shallower)
ones. A sibling ``AGENTS.override.md`` fully replaces that directory's
``AGENTS.md`` when present.

This module is deliberately **sync, filesystem-only** — no async, no network,
no LLM. That keeps it cheap to call at turn-build time and trivial to unit test
in isolation with a temp dir.

Public API:
  * :func:`find_project_root` — locate the nearest ancestor containing a marker.
  * :func:`load_project_instructions` — root→cwd AGENTS.md concatenation with a
    hard byte budget (truncating the last/cwd-most file).
  * :func:`project_instructions_fragment` — wrap the result in a
    :class:`~app.agents.context_fragments.ContextFragment`.
"""
from __future__ import annotations

from pathlib import Path

from app.agents.context_fragments import ContextFragment

# Marker files/dirs that identify a project root. ``.git`` covers normal repos
# and git worktrees (where ``.git`` is a file, not a dir — ``exists()`` handles
# both). Callers may pass their own tuple, e.g. ``(".git", ".hg", "pyproject.toml")``.
_DEFAULT_MARKERS: tuple[str, ...] = (".git",)


def find_project_root(
    start_path: str | Path,
    markers: tuple[str, ...] = _DEFAULT_MARKERS,
) -> Path | None:
    """Walk up from ``start_path`` until a directory containing any of
    ``markers`` is found; return that directory, or ``None`` at the filesystem
    root.

    ``start_path`` may be a file or a directory; if a file, its parent is used.
    Symlinks are resolved first (``Path.resolve()``) so marker lookup is stable.
    """
    p = Path(start_path).resolve()
    if p.is_file():
        p = p.parent

    while True:
        for marker in markers:
            if (p / marker).exists():
                return p
        # Reached the filesystem root without finding a marker.
        if p.parent == p:
            return None
        p = p.parent


def _instructions_chain(root: Path, cwd: Path) -> list[Path]:
    """The directories from ``root`` down to ``cwd`` (inclusive), root first.

    ``root`` must be ``cwd`` or an ancestor of it (as :func:`find_project_root`
    guarantees when called on ``cwd``). Returns ``[]`` if that invariant does
    not hold (e.g. the caller passed mismatched paths).
    """
    try:
        rel = cwd.relative_to(root)
    except ValueError:
        return []

    chain: list[Path] = [root]
    cur = root
    for part in rel.parts:
        cur = cur / part
        chain.append(cur)
    return chain


def _pick_instructions_file(directory: Path) -> Path | None:
    """Return this directory's effective instructions file, or ``None``.

    ``AGENTS.override.md`` takes precedence over a sibling ``AGENTS.md``
    (replaces it entirely). Existence is checked at call time.
    """
    override = directory / "AGENTS.override.md"
    if override.is_file():
        return override
    normal = directory / "AGENTS.md"
    if normal.is_file():
        return normal
    return None


def load_project_instructions(
    cwd: str | Path,
    *,
    max_bytes: int = 8192,
) -> str:
    """Load and concatenate every ``AGENTS.md`` from the project root down to
    ``cwd``.

    Order: **root → cwd** (shallowest first). A more-deeply-nested file is
    appended *after* shallower ones, so it appears later and can override them.
    ``AGENTS.override.md`` replaces its sibling ``AGENTS.md`` in the same dir.

    A hard ``max_bytes`` budget is enforced by truncating the *tail* of the
    concatenated text — because order is root→cwd, that tail lives in the
    last (cwd-most) file, so deeper instructions are the ones trimmed when the
    budget is tight. The result never exceeds ``max_bytes`` when UTF-8 encoded.

    Returns ``""`` when no project root is found (no marker) or no instructions
    file exists along the chain.
    """
    cwd_path = Path(cwd).resolve()
    root = find_project_root(cwd_path)
    if root is None:
        return ""

    pieces: list[str] = []
    for directory in _instructions_chain(root, cwd_path):
        path = _pick_instructions_file(directory)
        if path is None:
            continue
        # rstrip to drop trailing whitespace/newlines so the join separator is
        # the only thing between two files (cleaner, deterministic byte layout).
        pieces.append(path.read_text(encoding="utf-8").rstrip())

    if not pieces:
        return ""

    text = "\n\n".join(pieces)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # Slice the byte stream to the budget; ``errors="ignore"`` drops a partial
    # trailing multi-byte codepoint so the result stays valid UTF-8.
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def project_instructions_fragment(text: str) -> ContextFragment:
    """Wrap loaded instructions in a :class:`ContextFragment` tagged
    ``project_instructions``, mirroring how :mod:`context_fragments` defines the
    other fragments (e.g. :func:`~app.agents.context_fragments.recognized_intent_fragment`).

    An empty ``text`` yields a fragment with an empty body, which the assembler
    drops via its standard ``body.strip()`` rule — so callers can pass the raw
    loader output without gating on emptiness.
    """
    return ContextFragment(
        name="project_instructions",
        tag="project_instructions",
        body=text,
    )
