"""Prompt token budgeting and latest-turn admission."""
from __future__ import annotations

from dataclasses import dataclass
from math import floor, isfinite

from app.core.exceptions import AppException
from app.model_capabilities import ModelCapabilities


INVALID_PROMPT_BUDGET = "invalid_prompt_budget"
MESSAGE_TOO_LARGE = "message_too_large"


class PromptAdmissionError(AppException):
    """Expected rejection carrying a stable machine-readable admission code."""

    def __init__(self, code: str, message: str) -> None:
        status_code = 413 if code == MESSAGE_TOO_LARGE else 400
        super().__init__(status_code=status_code, code=code, message=message)


@dataclass(frozen=True, slots=True)
class TokenBudget:
    context_window: int
    requested_output_tokens: int
    reserved_output_tokens: int
    tool_schema_tokens: int
    safety_margin_tokens: int
    input_tokens: int

    @property
    def output_tokens(self) -> int:
        return self.reserved_output_tokens

    @property
    def safety_margin(self) -> int:
        return self.safety_margin_tokens


def _invalid(message: str) -> PromptAdmissionError:
    return PromptAdmissionError(INVALID_PROMPT_BUDGET, message)


def calculate_prompt_budget(
    caps: ModelCapabilities,
    requested_output: int,
    tool_schema_tokens: int = 0,
    safety_ratio: float = 0.05,
) -> TokenBudget:
    """Reserve output, tool schemas, and safety headroom from context."""

    if requested_output <= 0:
        raise _invalid("requested output tokens must be positive")
    if tool_schema_tokens < 0:
        raise _invalid("tool schema tokens cannot be negative")
    if not isfinite(safety_ratio) or safety_ratio < 0:
        raise _invalid("safety ratio cannot be negative")

    reserved_output = min(requested_output, caps.max_output_tokens)
    safety_margin = max(256, floor(caps.context_window * safety_ratio))
    input_tokens = caps.context_window - reserved_output - tool_schema_tokens - safety_margin
    if input_tokens <= 0:
        raise _invalid("model context leaves no positive prompt budget")

    return TokenBudget(
        context_window=caps.context_window,
        requested_output_tokens=requested_output,
        reserved_output_tokens=reserved_output,
        tool_schema_tokens=tool_schema_tokens,
        safety_margin_tokens=safety_margin,
        input_tokens=input_tokens,
    )


def admit_latest_turn(latest_turn_tokens: int, input_budget: int) -> None:
    """Reject a current message that cannot fit; never truncate it silently."""

    if latest_turn_tokens < 0 or input_budget <= 0:
        raise _invalid("token counts must produce a positive input budget")
    if latest_turn_tokens > input_budget:
        raise PromptAdmissionError(
            MESSAGE_TOO_LARGE,
            "The latest message is too large for this model's prompt budget",
        )


__all__ = [
    "PromptAdmissionError",
    "TokenBudget",
    "admit_latest_turn",
    "calculate_prompt_budget",
]
