"""World-state diffing for context fragments (Codex pattern).

Problem solved: re-injecting the full context (environment, project instructions,
mode, …) every turn re-pays its token cost AND busts any prompt-cache prefix,
because earlier messages get rewritten. Codex instead emits an *incremental
delta*: each fragment type has a serializable snapshot, and only sections whose
snapshot changed since the previous turn are re-injected.

This module provides the mechanism:
  * :class:`WorldStateDiffer` — holds the last rendered text per fragment name;
    :meth:`diff` returns only the fragments that changed and updates state.
  * :func:`differ_for` — a process-local cache of differs keyed by conversation
    id, so each conversation tracks its own baseline across turns.

The integration (which fragments get diffed into the prompt) lives in
``chat_service``; this module is pure + unit-testable.
"""
from __future__ import annotations

import threading
from collections.abc import Iterable

from app.agents.context_fragments import ContextFragment


class WorldStateDiffer:
    """Tracks each fragment name's last rendered text; emits only changed ones.

    Call :meth:`diff` once per turn with the full assembled fragment set; it
    returns the subset whose rendered block changed (or is new), and updates the
    stored snapshot. A fragment that disappears this round is dropped from the
    snapshot, so if it reappears later it re-emits (Codex emits a ``status=
    unavailable`` tombstone; v1 simply re-emits on return — tombstones are a
    future refinement).
    """

    def __init__(self) -> None:
        self._last: dict[str, str] = {}

    def diff(self, fragments: Iterable[ContextFragment]) -> list[ContextFragment]:
        current: dict[str, str] = {}
        changed: list[ContextFragment] = []
        for frag in fragments:
            rendered = frag.render()
            current[frag.name] = rendered
            # Emit only when the rendered block actually differs AND is non-empty
            # (empty bodies never produce a visible block to inject).
            if rendered and rendered != self._last.get(frag.name):
                changed.append(frag)
        self._last = current
        return changed

    def snapshot(self) -> dict[str, str]:
        """A copy of the current per-name rendered snapshot (for inspection/tests)."""
        return dict(self._last)

    def reset(self) -> None:
        self._last.clear()

    def diff_with_retained(
        self,
        fragments: Iterable[ContextFragment],
        retained_messages: list[dict] | None = None,
    ) -> list[ContextFragment]:
        """Diff, then additionally drop fragments already present in retained history.

        Codex's ``render_history_diff(prev, retained)`` checks whether a section's
        rendered block already lives in the retained message text before
        re-injecting — so a fragment carried over by compaction isn't duplicated.
        Uses :meth:`ContextFragment.contains_tag` (stable-marker identity).
        """
        changed = self.diff(fragments)
        if not retained_messages:
            return changed
        blob = "\n\n".join(_message_text(m) for m in retained_messages)
        return [f for f in changed if not ContextFragment.contains_tag(blob, f.tag)]


def _message_text(message: dict) -> str:
    """Flatten a message dict to its text content (for tag-presence checks)."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


# --------------------------------------------------------------------------- #
# Per-conversation differ cache (process-local). Long-running backends may run
# multiple worker processes; each keeps its own baseline. A turn that lands on a
# different worker simply re-emits once (a safe superset), then re-establishes
# the baseline — correctness is preserved, only the first turn after a hop loses
# the diffing benefit.
# --------------------------------------------------------------------------- #
_DIFFERS: dict[str, WorldStateDiffer] = {}
_DIFFERS_LOCK = threading.Lock()


def differ_for(conversation_id: str) -> WorldStateDiffer:
    """Get (or create) the :class:`WorldStateDiffer` for a conversation."""
    with _DIFFERS_LOCK:
        d = _DIFFERS.get(conversation_id)
        if d is None:
            d = WorldStateDiffer()
            _DIFFERS[conversation_id] = d
        return d


def drop_differ(conversation_id: str) -> None:
    """Forget the baseline for a conversation (e.g. on delete)."""
    with _DIFFERS_LOCK:
        _DIFFERS.pop(conversation_id, None)
