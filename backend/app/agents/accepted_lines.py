"""Accepted-line fingerprints from a unified diff (Codex pattern).

Measures edit acceptance: which AI-suggested lines the user actually kept. Codex
fingerprints each added line (``+``) of the proposed diff so a later diff of what
the user saved can be matched back, yielding an acceptance rate — a concrete
quality signal for code-editing agents.

Pure string parsing; no VCS needed.
"""
from __future__ import annotations

import hashlib
import re


def fingerprint_hash(line: str) -> str:
    """Stable fingerprint of a code line (whitespace-normalized so re-indentation
    doesn't change the fingerprint)."""
    norm = " ".join((line or "").split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


_HUNK_HEADER = re.compile(r"^@@.*@@")


def accepted_line_fingerprints_from_unified_diff(diff_text: str) -> list[str]:
    """Return fingerprints of the ADDED lines (``+``) in a unified diff.

    Skips file headers (``+++``/``---``) and hunk headers (``@@``). De-duplicated,
    order-preserving. Empty/whitespace-only lines are skipped.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in (diff_text or "").splitlines():
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if _HUNK_HEADER.match(raw):
            continue
        if raw.startswith("+"):
            line = raw[1:]
            if not line.strip():
                continue
            fp = fingerprint_hash(line)
            if fp not in seen:
                seen.add(fp)
                out.append(fp)
    return out
