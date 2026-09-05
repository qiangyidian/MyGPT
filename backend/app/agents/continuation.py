"""Bounded automatic-continuation primitives and model-usage accounting."""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from numbers import Real
from typing import Any

DEFAULT_COMPARISON_WINDOW = 4_096
MAX_CONTINUATION_ROUNDS = 8


@dataclass(frozen=True, slots=True)
class ContinuationPolicy:
    """Immutable bounds for automatic follow-up model rounds."""

    max_rounds: int = 2
    comparison_window: int = DEFAULT_COMPARISON_WINDOW

    def __post_init__(self) -> None:
        if isinstance(self.max_rounds, bool) or not isinstance(self.max_rounds, int):
            raise ValueError("max_rounds must be an integer")
        if not 0 <= self.max_rounds <= MAX_CONTINUATION_ROUNDS:
            raise ValueError(
                f"max_rounds must be between 0 and {MAX_CONTINUATION_ROUNDS}"
            )
        if not 64 <= self.comparison_window <= 65_536:
            raise ValueError("comparison_window must be between 64 and 65536")

    def should_continue(
        self,
        finish_reason: str | None,
        round_number: int,
        *,
        pending_tool_calls: bool = False,
        cancelled: bool = False,
    ) -> bool:
        """Whether another bounded, text-only provider round is permitted."""
        return bool(
            finish_reason == "length"
            and round_number < self.max_rounds
            and not pending_tool_calls
            and not cancelled
        )


def _suffix_prefix_length(existing: str, prefix: str) -> int:
    """Return the longest suffix(existing) == prefix(prefix), in linear time."""
    if not existing or not prefix:
        return 0
    failure = [0] * len(prefix)
    matched = 0
    for index in range(1, len(prefix)):
        while matched and prefix[index] != prefix[matched]:
            matched = failure[matched - 1]
        if prefix[index] == prefix[matched]:
            matched += 1
        failure[index] = matched

    matched = 0
    last_index = len(existing) - 1
    for index, char in enumerate(existing):
        while matched and char != prefix[matched]:
            matched = failure[matched - 1]
        if char == prefix[matched]:
            matched += 1
            if matched == len(prefix) and index != last_index:
                # A full match before the end cannot be the final suffix. Keep
                # its longest reusable border while scanning the remaining text.
                matched = failure[matched - 1]
    return matched


def _normalise_whitespace(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace and map normalized characters to original end offsets."""
    normalized: list[str] = []
    original_ends: list[int] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            end = index + 1
            while end < len(text) and text[end].isspace():
                end += 1
            normalized.append(" ")
            original_ends.append(end)
            index = end
            continue
        normalized.append(char)
        original_ends.append(index + 1)
        index += 1
    return "".join(normalized), original_ends


def continuation_novel_text(
    existing: str,
    continuation: str,
    *,
    comparison_window: int = DEFAULT_COMPARISON_WINDOW,
) -> str:
    """Return only new continuation text after bounded overlap removal."""
    if not continuation:
        return ""
    if not existing:
        return continuation
    # Exact repeats are common and cheap to recognize even when larger than the
    # comparison window. This remains linear and does not introduce quadratic work.
    if existing.endswith(continuation):
        return ""

    window = max(1, comparison_window)
    existing_suffix = existing[-window:]
    continuation_prefix = continuation[:window]
    exact = _suffix_prefix_length(existing_suffix, continuation_prefix)
    cut = exact

    normalized_existing, _ = _normalise_whitespace(existing_suffix)
    normalized_continuation, continuation_ends = _normalise_whitespace(
        continuation_prefix
    )
    normalized_overlap = _suffix_prefix_length(
        normalized_existing, normalized_continuation
    )
    if normalized_overlap:
        repeated = normalized_continuation[:normalized_overlap].strip()
        # A whitespace-only or one-character match is too weak to justify
        # deleting formatting from a genuinely new continuation.
        if len(repeated) >= 2:
            normalized_cut = continuation_ends[normalized_overlap - 1]
            cut = max(cut, normalized_cut)
    return continuation[cut:]


def merge_continuation(
    existing: str,
    continuation: str,
    *,
    comparison_window: int = DEFAULT_COMPARISON_WINDOW,
) -> str:
    """Merge a model continuation without repeating its bounded overlap."""
    return existing + continuation_novel_text(
        existing, continuation, comparison_window=comparison_window
    )


class ContinuationBuffer:
    """Buffer only the overlap-sized prefix, then stream novel text directly."""

    def __init__(
        self,
        existing: str,
        *,
        comparison_window: int = DEFAULT_COMPARISON_WINDOW,
    ) -> None:
        self._existing = existing
        self._window = max(1, comparison_window)
        self._prefix = ""
        self._resolved = False

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        if self._resolved:
            return chunk
        needed = self._window - len(self._prefix)
        self._prefix += chunk[:needed]
        remainder = chunk[needed:]
        if len(self._prefix) < self._window:
            return ""
        self._resolved = True
        return continuation_novel_text(
            self._existing,
            self._prefix,
            comparison_window=self._window,
        ) + remainder

    def flush(self) -> str:
        if self._resolved:
            return ""
        self._resolved = True
        return continuation_novel_text(
            self._existing,
            self._prefix,
            comparison_window=self._window,
        )


def _safe_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return int(number) if number.is_integer() else number


def aggregate_usage(
    rounds: Iterable[dict[str, Any] | None],
) -> dict[str, int | float] | None:
    """Sum one final usage snapshot per model round.

    Standard counters and safe numeric extension fields are retained. Nested
    OpenAI detail counters are flattened without counting them again when a
    provider also supplies the corresponding top-level field.
    """
    aggregate: dict[str, int | float] = {}
    for raw in rounds:
        if not isinstance(raw, dict):
            continue
        current: dict[str, int | float] = {}
        for key, value in raw.items():
            safe = _safe_number(value)
            if safe is not None:
                current[str(key)] = safe

        prompt_details = raw.get("prompt_tokens_details")
        if "cached_tokens" not in current and isinstance(prompt_details, dict):
            cached = _safe_number(prompt_details.get("cached_tokens"))
            if cached is not None:
                current["cached_tokens"] = cached
        completion_details = raw.get("completion_tokens_details")
        if "reasoning_tokens" not in current and isinstance(completion_details, dict):
            reasoning = _safe_number(completion_details.get("reasoning_tokens"))
            if reasoning is not None:
                current["reasoning_tokens"] = reasoning

        if "total_tokens" not in current and (
            "prompt_tokens" in current or "completion_tokens" in current
        ):
            current["total_tokens"] = current.get("prompt_tokens", 0) + current.get(
                "completion_tokens", 0
            )

        for key, value in current.items():
            aggregate[key] = aggregate.get(key, 0) + value
    return aggregate or None


def normalize_usage(raw: dict[str, Any] | None) -> dict[str, int | float] | None:
    """Retain only finite, non-negative numeric metering fields."""
    return aggregate_usage([raw])
