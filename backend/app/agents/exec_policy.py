"""Exec-policy engine: allow / prompt / forbidden command-prefix rules.

A Codex-style command-gating policy. A command's tokenized argv is matched
against an ordered list of :class:`PrefixRule` patterns; the FIRST pattern that
is a prefix of argv wins and its decision is returned. With no match the policy
``default`` applies (``"prompt"`` out of the box, so unknown commands still ask
before running).

This module is deliberately self-contained: stdlib only (``json`` / ``pathlib``
/ ``dataclasses`` / ``typing``). It is safe to import from sync test code and
from the runtime alike. Persistence (:class:`RuleStore`) is a flat JSON file
written atomically via tmp-file + ``os.replace`` so a crash mid-write never
leaves a half-written policy on disk.

Design notes:
  * Patterns are *concrete* token prefixes -- ``["git", "status"]`` matches
    ``["git", "status"]`` and ``["git", "status", "--short"]`` but not
    ``["git", "push"]``. Matching is case-sensitive on the command name and
    exact on every argument (plain slice equality).
  * :func:`validate_pattern` keeps user-supplied patterns concrete: empty
    patterns and glob/wildcard tokens (``*``, ``?``, ``://``) are rejected, so a
    remembered approval can never widen into a blanket ``*`` permit.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

Decision = Literal["allow", "prompt", "forbidden"]

# Default decision returned by ExecPolicy/RuleStore when no rule matches.
DEFAULT_DECISION: Decision = "prompt"

# Tokens that must never appear inside a concrete prefix pattern -- they would
# turn a remembered approval into an overly-broad glob, defeating the safety
# model. Substring-matched so e.g. "status?" or "http://..." are also caught.
_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = ("*", "?", "://")


@dataclass
class PrefixRule:
    """A single concrete command-prefix rule.

    ``pattern`` is a list of argv tokens; it matches an argv when it is a
    prefix of that argv (case-sensitive, exact on every token). ``decision`` is
    what the policy returns when this rule is the first to match.
    """

    pattern: list[str]
    decision: Decision


def validate_pattern(pattern: list[str]) -> None:
    """Reject patterns that are empty or contain glob/wildcard tokens.

    Raises ``ValueError`` for:
      * empty patterns (``[]``) -- they would match every command, and
        represent a misconfiguration rather than intent;
      * any token containing ``*``, ``?`` or ``://`` -- these turn a remembered
        approval into a blanket glob, which the safety model forbids.

    Concrete commands and flags pass unchanged.
    """
    if not pattern:
        raise ValueError("pattern must not be empty")
    for tok in pattern:
        if not isinstance(tok, str):
            raise ValueError(f"pattern tokens must be str, got {type(tok).__name__}")
        for bad in _FORBIDDEN_SUBSTRINGS:
            if bad in tok:
                raise ValueError(
                    f"pattern token {tok!r} contains forbidden wildcard/glob "
                    f"substring {bad!r}; patterns must be concrete tokens"
                )


@dataclass
class ExecPolicy:
    """An ordered set of prefix rules evaluated first-match-wins."""

    rules: list[PrefixRule] = field(default_factory=list)
    default: Decision = DEFAULT_DECISION

    def decide(self, argv: list[str]) -> Decision:
        """Return the decision for ``argv``: first matching rule wins.

        A rule matches when its ``pattern`` is a prefix of ``argv`` (same
        length-or-shorter argv rejected, then exact token-by-token equality on
        the leading slice). When no rule matches, :attr:`default` is returned.
        """
        for rule in self.rules:
            pat = rule.pattern
            if len(pat) > len(argv):
                continue
            if argv[: len(pat)] == pat:
                return rule.decision
        return self.default


class RuleStore:
    """JSON-backed persistence for an :class:`ExecPolicy`.

    File shape (pretty-printed, UTF-8)::

        {
          "default": "prompt",
          "rules": [
            {"pattern": ["git", "status"], "decision": "allow"}
          ]
        }

    Writes are atomic: the policy is serialized to ``<path>.tmp`` then
    ``os.replace`` swaps it into place, so a crash never yields a truncated
    file. No advisory locking is used; concurrent writers would simply
    last-writer-wins on the atomic replace (acceptable for the single-operator
    approval memory this is designed for).
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # -- reading -----------------------------------------------------------
    def load(self) -> ExecPolicy:
        """Load the policy from disk.

        A missing file yields a fresh empty policy with the default decision
        (first-run friendly). A present-but-malformed file is allowed to raise
        ``json.JSONDecodeError`` / ``KeyError`` rather than silently masking a
        real corruption.
        """
        if not self.path.exists():
            return ExecPolicy(default=DEFAULT_DECISION)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        rules = [
            PrefixRule(pattern=list(r["pattern"]), decision=r["decision"])
            for r in data.get("rules", [])
        ]
        default = data.get("default", DEFAULT_DECISION)
        return ExecPolicy(rules=rules, default=default)

    # -- writing -----------------------------------------------------------
    def add_allow_prefix(self, pattern: list[str]) -> ExecPolicy:
        """Append a remembered ``allow`` rule for ``pattern`` and persist it.

        Validates the pattern first (no globs / non-empty), then dedups against
        any existing rule with the same pattern AND decision so repeatedly
        approving the same command does not bloat the file. The updated policy
        is written atomically and the freshly-loaded policy is returned.
        """
        validate_pattern(pattern)
        policy = self.load()
        pat = list(pattern)
        if any(r.pattern == pat and r.decision == "allow" for r in policy.rules):
            # Already remembered identically; nothing to do, but still return a
            # freshly loaded view so callers see stable post-conditions.
            return policy
        policy.rules.append(PrefixRule(pattern=pat, decision="allow"))
        self._save(policy)
        return self.load()

    def _save(self, policy: ExecPolicy) -> None:
        """Serialize ``policy`` to a tmp file then atomically replace the target."""
        payload = {
            "default": policy.default,
            "rules": [asdict(r) for r in policy.rules],
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        tmp = self.path.with_name(self.path.name + ".tmp")
        # Create parent dir if needed (first write into a fresh path).
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        # os.replace is atomic on both POSIX and Windows (overwrites target).
        os.replace(tmp, self.path)
