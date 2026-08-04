"""Reasoning-effort control (Codex pattern).

One knob resolved in three layers: explicit user override → the model's default
effort → the efforts the model actually supports. A ``Custom`` escape hatch
absorbs unknown future effort names (the server invents a new tier) instead of
erroring, so a newer backend never breaks an older client.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# Canonical effort tiers (Codex's ladder). Anything else parses to a custom tier.
CANONICAL_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


@dataclass(frozen=True)
class ReasoningPreset:
    effort: str          # canonical id or "custom:..."
    description: str = ""


@dataclass(frozen=True)
class ModelReasoningCatalog:
    """Per-model reasoning capability: which efforts are supported + the default."""

    default: str
    supported: tuple[ReasoningPreset, ...]

    @property
    def supported_ids(self) -> set[str]:
        return {p.effort for p in self.supported}


def parse_effort(value: str | None) -> str | None:
    """Canonicalize an effort string; unknown values become ``custom:<raw>``."""
    if value is None:
        return None
    v = value.strip().lower()
    if not v:
        return None
    if v in CANONICAL_EFFORTS:
        return v
    return f"custom:{v}"


def resolve_effort(
    user_override: str | None,
    model_default: str | None,
    supported: Iterable[str] | ModelReasoningCatalog | None,
) -> str | None:
    """Resolve the effective effort for a turn.

    Order: explicit user override (if supported, or custom) → model default (if
    supported) → None (model/provider applies its own default).
    """
    supported_ids = (
        supported.supported_ids
        if isinstance(supported, ModelReasoningCatalog)
        else (set(supported) if supported is not None else None)
    )

    override = parse_effort(user_override)
    if override is not None:
        # Custom efforts are always allowed (forward-compat); canonical only if supported.
        if override.startswith("custom:") or supported_ids is None or override in supported_ids:
            return override

    # No (usable) override -> fall back to the model default. If the caller didn't
    # pass one explicitly, use the catalog's declared default.
    default_raw = model_default
    if default_raw is None and isinstance(supported, ModelReasoningCatalog):
        default_raw = supported.default
    default = parse_effort(default_raw)
    if default is not None and (supported_ids is None or default in supported_ids):
        return default
    return None
