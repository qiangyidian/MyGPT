"""comp_hash-gated re-compaction on model switch (Codex pattern).

Problem: switching models mid-conversation can hand the new model a transcript
sized for the OLD model's context window — it either errors on overflow or gets
silently truncated. Codex fixes this with a ``comp_hash``: a tiny fingerprint of
"how this model summarises / its context shape". When the hash changes across a
switch, compaction runs under the *previous* model first (so the old model
summarises its own history in a compatible shape) before the new model takes over.

This module is the pure decision logic (testable without an LLM); the wiring
into the compaction runtime is the integration point.
"""
from __future__ import annotations

import hashlib


def comp_hash(*, provider: str, model_name: str, context_window_tokens: int, **extras: object) -> str:
    """A stable fingerprint of 'how this model compacts' (provider + model + window).

    Extra capability signals (e.g. supports_reasoning) can be folded in via
    ``extras`` so two same-name models with different capabilities hash apart.
    """
    parts = [str(provider or ""), str(model_name or ""), str(int(context_window_tokens or 0))]
    for k in sorted(extras):
        parts.append(f"{k}={extras[k]}")
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def should_recompact_on_switch(previous_hash: str | None, current_hash: str | None) -> bool:
    """True when the compaction fingerprint differs across a model switch."""
    if not previous_hash or not current_hash:
        return False
    return previous_hash != current_hash


def is_downshift(
    *, previous_window_tokens: int, current_window_tokens: int, active_tokens: int
) -> bool:
    """True when the new window is smaller AND the active transcript already exceeds it.

    A downshift is the dangerous case: compaction MUST run or the new model overflows.
    """
    if previous_window_tokens <= 0 or current_window_tokens <= 0:
        return False
    return current_window_tokens < previous_window_tokens and active_tokens >= current_window_tokens
