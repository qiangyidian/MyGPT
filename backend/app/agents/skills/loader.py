"""Skills loader — discover ``SKILL.md`` files and resolve ``$name`` mentions.

Modeled on Codex's skill-injection pattern: each ``SKILL.md`` carries a tiny
YAML frontmatter (``name`` / ``description``) plus a markdown body. At turn
time we scan the user's text for ``$skill-name`` mentions (bare or as markdown
links ``[$name](skill://...)``) and inject the matching bodies as context
fragments so the model can act on the skill without the user pasting it.

Stdlib only (``pathlib`` / ``re`` / ``dataclasses``) — no PyYAML dependency.
The frontmatter these files use is flat ``key: value``, so a hand-rolled
parser is both smaller and more robust than pulling in a YAML lib.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.agents.context_fragments import ContextFragment

# Per-skill body size cap. Mirrors Codex's guard against one giant skill
# blowing the context budget — truncate rather than reject.
MAX_BODY_BYTES = 16 * 1024

# A skill mention: ``$`` immediately followed by a run of word chars / dash /
# underscore. Catches both the bare form (``$git-commit``) and the markdown
# link form (``[$git-commit](skill://...)``), since the latter still contains
# the literal ``$name`` token inside the brackets. False positives (e.g.
# ``cost $5``) are harmless: non-existent names are dropped at resolve time.
_MENTION_RE = re.compile(r"\$([A-Za-z0-9_-]+)")

# Frontmatter closing delimiter — a line whose stripped content is exactly
# ``---``. The opening delimiter is handled by the caller (first-line check).
_FRONTMATTER_DELIM = "---"


# --------------------------------------------------------------------------- #
# Value object
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Skill:
    """One discovered skill. ``source`` is the SKILL.md path it came from."""

    name: str
    description: str
    body: str
    source: Path


# --------------------------------------------------------------------------- #
# Frontmatter parsing
# --------------------------------------------------------------------------- #
def _strip_quotes(value: str) -> str:
    """Drop a single matching pair of surrounding single/double quotes."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse a leading ``---\\n<yaml>\\n---\\n<body>`` block.

    Returns ``(meta, body)``. ``meta`` is a flat ``key -> value`` dict of the
    lines between the delimiters (values de-quoted). If there is no leading
    ``---`` opener, or the block never closes, the whole ``text`` is returned
    as the body and ``meta`` is ``{}``.

    Only flat ``key: value`` lines are understood (no nesting, no lists) —
    that is all these files use. Unknown keys are kept (lenient); consumers
    only read ``name`` / ``description``.
    """
    if not text:
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        return {}, text

    meta: dict = {}
    close_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMATTER_DELIM:
            close_idx = i
            break
        line = lines[i]
        if not line.strip():
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = _strip_quotes(value.strip())

    if close_idx == -1:
        # Opener present but no closer — treat as no frontmatter (lenient).
        return {}, text

    body = "\n".join(lines[close_idx + 1:])
    return meta, body


def _cap_body_bytes(body: str, max_bytes: int = MAX_BODY_BYTES) -> str:
    """Truncate ``body`` to at most ``max_bytes`` of UTF-8.

    Truncates at the byte boundary and drops any dangling partial codepoint
    (``errors="ignore"``) so the result is always valid UTF-8.
    """
    encoded = body.encode("utf-8")
    if len(encoded) <= max_bytes:
        return body
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_skills(roots: list[Path]) -> dict[str, Skill]:
    """Glob each root for ``**/SKILL.md``, parse, and index by skill ``name``.

    Roots are processed in order; a skill ``name`` found in a later root
    overrides one with the same name from an earlier root (last-wins). Files
    missing a ``name`` in their frontmatter are skipped. Bodies larger than
    :data:`MAX_BODY_BYTES` are truncated.
    """
    index: dict[str, Skill] = {}
    for root in roots:
        if not root or not root.exists():
            continue
        for path in sorted(root.rglob("SKILL.md")):
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta, body = _parse_frontmatter(raw)
            name = meta.get("name", "").strip()
            if not name:
                continue
            description = meta.get("description", "").strip()
            index[name] = Skill(
                name=name,
                description=description,
                body=_cap_body_bytes(body),
                source=path,
            )
    return index


# --------------------------------------------------------------------------- #
# Mention resolution
# --------------------------------------------------------------------------- #
def resolve_mentions(text: str, skills: dict[str, Skill]) -> list[Skill]:
    """Find ``$name`` / ``[$name](skill://...)`` mentions and return known skills.

    Mentions are deduplicated preserving first-seen order. Unknown names
    (no matching skill) are silently ignored.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[Skill] = []
    for match in _MENTION_RE.finditer(text):
        name = match.group(1)
        if name in seen:
            continue
        # Record every mention as seen so a later repeat never re-resolves
        # (dedup is by mention string, which maps 1:1 to a skill name).
        seen.add(name)
        if name in skills:
            out.append(skills[name])
    return out


# --------------------------------------------------------------------------- #
# Fragment adapter
# --------------------------------------------------------------------------- #
def skill_fragment(skill: Skill) -> ContextFragment:
    """Wrap a skill's body as a :class:`ContextFragment` for per-turn injection."""
    return ContextFragment(
        name="skill",
        tag="skill",
        body=f"# Skill: {skill.name}\n{skill.body}",
    )
