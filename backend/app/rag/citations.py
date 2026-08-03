"""Citation integrity: keep ``[source N]`` markers honest.

The system prompt / RAG context encourage the model to cite sources as
``[source N]``, and the deterministic demo writer emits them too. A marker is
only truthful when there is a real :class:`~app.schemas.Citation` for that
number — otherwise it fabricates a source (the regression that made the canned
demo answer look like it had two backing citations it never had).

:func:`sanitize_unbacked_source_markers` strips any ``[source N]`` (and the
Chinese ``[来源 N]``) whose ``N`` has no matching citation, and reports whether
it changed anything so the caller can flag the turn as
``citation_validation_failed``.
"""
from __future__ import annotations

import re

# Matches "[source 1]", "[Source 12]", "[来源 3]", and the no-space / colon
# variants a model might emit ("[source1]", "[source: 1]", "[来源：1]") —
# case-insensitive, tolerant of internal whitespace / colons.
_SOURCE_MARKER = re.compile(r"\[\s*(?:source|来源)[\s:：]*(\d+)\s*\]", re.IGNORECASE)


def sanitize_unbacked_source_markers(
    text: str | None, citation_count: int
) -> tuple[str, bool]:
    """Return ``(sanitized_text, changed)``.

    A marker ``[source N]`` is kept only when ``1 <= N <= citation_count``
    (there is a real citation for it); every other marker is removed and the
    surrounding whitespace tidied. ``changed`` is True iff at least one unbacked
    marker was stripped, so callers can mark the turn as
    ``citation_validation_failed``.

    With ``citation_count == 0`` every marker is unbacked, so the result
    contains no ``[source N]`` at all.
    """
    if not text:
        return (text or ""), False

    changed = False

    def _repl(match: re.Match[str]) -> str:
        nonlocal changed
        n = int(match.group(1))
        if citation_count > 0 and 1 <= n <= citation_count:
            return match.group(0)  # backed by a real citation — keep verbatim
        changed = True
        return ""  # unbacked — strip the fabricated marker

    out = _SOURCE_MARKER.sub(_repl, text)
    if changed:
        # Collapse the double spaces a removed inline marker can leave behind
        # and tidy a stray space before punctuation.
        out = re.sub(r"[ \t]{2,}", " ", out)
        out = re.sub(r"\s+([，。、；;：:！？!?,.])", r"\1", out)
        out = out.strip()
    return out, changed
