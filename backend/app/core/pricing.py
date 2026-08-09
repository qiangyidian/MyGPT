"""Model pricing + cost computation.

Parses ``MODEL_PRICING_JSON`` (a map of model-substring → {prompt, completion}
USD-per-1M-tokens) and computes the USD cost of a usage payload against the
longest matching substring. Kept tiny and dependency-free. When no table is
configured (or no entry matches), cost is None — usage is still recorded.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _pricing_table() -> list[tuple[str, float, float]]:
    """Return [(substring, prompt_per_1m, completion_per_1m)], longest-first."""
    raw = (get_settings().MODEL_PRICING_JSON or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("MODEL_PRICING_JSON is not valid JSON; cost accounting disabled")
        return []
    out: list[tuple[str, float, float]] = []
    for key, val in (data or {}).items():
        if not isinstance(val, dict):
            continue
        try:
            out.append((str(key).lower(), float(val.get("prompt", 0)), float(val.get("completion", 0))))
        except (TypeError, ValueError):
            continue
    # Longest substring first so "gpt-4o-mini" matches before "gpt-4o".
    out.sort(key=lambda t: len(t[0]), reverse=True)
    return out


def reset_pricing_cache() -> None:
    """Drop the cached pricing table (test / config-reload hook)."""
    _pricing_table.cache_clear()


def compute_cost(model_name: str | None, usage: dict[str, Any] | None) -> float | None:
    """Return USD cost for a usage payload, or None if unpriced/unmatched.

    ``usage`` is the provider dict {prompt_tokens, completion_tokens[, total_tokens]}.
    """
    if not usage or not model_name:
        return None
    table = _pricing_table()
    if not table:
        return None
    name = model_name.lower()
    match = next((row for row in table if row[0] and row[0] in name), None)
    if match is None:
        return None
    _substr, p_per_1m, c_per_1m = match
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    return round((prompt / 1_000_000.0) * p_per_1m + (completion / 1_000_000.0) * c_per_1m, 6)


def normalize_usage(usage: dict[str, Any] | None) -> dict[str, int | None] | None:
    """Pull {prompt_tokens, completion_tokens, total_tokens} out of a provider payload."""
    if not usage:
        return None
    pt = usage.get("prompt_tokens")
    ct = usage.get("completion_tokens")
    tt = usage.get("total_tokens")
    if tt is None and (pt or ct):
        tt = (pt or 0) + (ct or 0)
    if pt is None and ct is None and tt is None:
        return None
    return {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": tt}
