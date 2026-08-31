"""Canonical model capability contract and legacy-config conversion."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


OutputTokenParameter = Literal["max_tokens", "max_completion_tokens"]

_DEFAULT_CONTEXT_WINDOW = 131072
_DEFAULT_MAX_OUTPUT_TOKENS = 8192
_OUTPUT_TOKEN_PARAMETERS = {"max_tokens", "max_completion_tokens"}


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """Immutable capabilities used by admission and provider adaptation.

    Unknown capabilities deliberately default to ``False``. This keeps an
    unrecognised or legacy model from being offered tools or modalities that
    it has not explicitly declared.
    """

    context_window: int
    max_output_tokens: int
    supports_tools: bool = False
    supports_parallel_tools: bool = False
    supports_vision: bool = False
    supports_audio_input: bool = False
    supports_audio_output: bool = False
    supports_image_generation: bool = False
    supports_structured_output: bool = False
    supports_reasoning_effort: bool = False
    output_token_parameter: OutputTokenParameter = "max_tokens"

    def __post_init__(self) -> None:
        if self.context_window <= 0:
            raise ValueError("context_window must be positive")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.output_token_parameter not in _OUTPUT_TOKEN_PARAMETERS:
            raise ValueError("unsupported output token parameter")

    @classmethod
    def from_model_config(cls, config: Any) -> "ModelCapabilities":
        return capabilities_from_config(config)


UNKNOWN_MODEL_CAPABILITIES = ModelCapabilities(
    context_window=_DEFAULT_CONTEXT_WINDOW,
    max_output_tokens=_DEFAULT_MAX_OUTPUT_TOKENS,
)


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def capabilities_from_config(config: Any) -> ModelCapabilities:
    """Convert an ORM/config-shaped object into the canonical contract.

    The established ``max_context_tokens`` and ``max_tokens`` fields remain
    authoritative, so legacy rows retain exactly the same limits.
    """

    output_parameter = getattr(config, "output_token_parameter", "max_tokens")
    if output_parameter not in _OUTPUT_TOKEN_PARAMETERS:
        output_parameter = "max_tokens"
    return ModelCapabilities(
        context_window=_positive_int(
            getattr(config, "max_context_tokens", None), _DEFAULT_CONTEXT_WINDOW
        ),
        max_output_tokens=_positive_int(
            getattr(config, "max_tokens", None), _DEFAULT_MAX_OUTPUT_TOKENS
        ),
        supports_tools=bool(getattr(config, "supports_tools", False)),
        supports_parallel_tools=bool(getattr(config, "supports_parallel_tools", False)),
        supports_vision=bool(getattr(config, "supports_vision", False)),
        supports_audio_input=bool(getattr(config, "supports_audio_input", False)),
        supports_audio_output=bool(getattr(config, "supports_audio_output", False)),
        supports_image_generation=bool(getattr(config, "supports_image_generation", False)),
        supports_structured_output=bool(getattr(config, "supports_structured_output", False)),
        supports_reasoning_effort=bool(getattr(config, "supports_reasoning_effort", False)),
        output_token_parameter=output_parameter,
    )


get_model_capabilities = capabilities_from_config


__all__ = [
    "ModelCapabilities",
    "OutputTokenParameter",
    "UNKNOWN_MODEL_CAPABILITIES",
    "capabilities_from_config",
    "get_model_capabilities",
]
