"""Model provider abstraction. Every LLM/embedding call goes through a ModelProvider.

Implementations live alongside (openai_compatible.py, mock.py). Business code never
constructs raw HTTP to a model — it asks the registry for a provider.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any, AsyncIterator, Literal

from app.agents.token_budget import (
    INVALID_PROMPT_BUDGET,
    PROMPT_TOO_LARGE,
    PromptAdmissionError,
    calculate_prompt_budget,
)
from app.model_capabilities import ModelCapabilities, UNKNOWN_MODEL_CAPABILITIES


# Canonical termination reasons carried end-to-end (provider → runtime → SSE →
# persistence → UI). Extend this, never pass arbitrary strings.
FinishReason = Literal[
    "stop",
    "length",
    "tool_calls",
    "cancelled",
    "timeout",
    "content_filter",
    "provider_error",
    "stream_disconnected",
    "budget",
    "error",
]

# Stable machine codes for provider-side failures, so upstream can map them to a
# FinishReason (e.g. "provider_timeout") instead of a generic "error".
PROVIDER_ERR_TIMEOUT = "provider_timeout"
PROVIDER_ERR_NETWORK = "provider_error"
PROVIDER_ERR_AUTH = "provider_auth"

_PROTECTED_CHAT_EXTRA_KEYS = {
    "max_completion_tokens",
    "max_tokens",
    "messages",
    "model",
    "stop",
    "stream",
    "stream_options",
    "temperature",
    "tool_choice",
    "tools",
    "top_p",
}


class ProviderError(RuntimeError):
    """Raised on transport/timeout/auth errors talking to a model endpoint.

    `code` is a stable machine string (see PROVIDER_ERR_*) so callers can map the
    failure to a specific FinishReason rather than a generic "error".
    """

    def __init__(self, message: str, *, code: str = PROVIDER_ERR_NETWORK) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ChatOptions:
    temperature: float = 0.7
    top_p: float = 1.0
    # None requests the provider's canonical maximum; final admission resolves it
    # to a concrete, capability-bounded value before dispatch.
    max_tokens: int | None = 1024
    output_token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    tools: list[dict[str, Any]] | None = None        # OpenAI tool schemas
    tool_choice: Any = "auto"
    stop: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.output_token_parameter not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("unsupported output token parameter")


@dataclass
class ToolCallDef:
    id: str
    name: str
    arguments: str            # raw JSON string (OpenAI convention)


@dataclass
class ChatDelta:
    """One streaming chunk."""
    content: str = ""
    tool_calls: list[ToolCallDef] | None = None
    finish_reason: FinishReason | None = None
    # Token usage — populated on the final usage-only chunk when streaming with
    # include_usage (OpenAI-compatible), or on ChatResult for non-streaming.
    usage: dict[str, int] | None = None


@dataclass
class ChatResult:
    """Non-streaming result."""
    content: str = ""
    tool_calls: list[ToolCallDef] | None = None
    finish_reason: FinishReason = "stop"
    usage: dict[str, int] | None = None   # {prompt_tokens, completion_tokens, total_tokens}


class ModelProvider(ABC):
    """Base for all model providers."""

    provider_name: str = "base"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str = "",
        output_token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens",
        capabilities: ModelCapabilities | None = None,
        **_: Any,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.output_token_parameter = (
            output_token_parameter
            if output_token_parameter in ("max_tokens", "max_completion_tokens")
            else "max_tokens"
        )
        self.capabilities = capabilities or UNKNOWN_MODEL_CAPABILITIES

    @abstractmethod
    async def chat(self, messages: list[dict[str, Any]], options: ChatOptions | None = None) -> ChatResult: ...

    @abstractmethod
    def stream_chat(
        self, messages: list[dict[str, Any]], options: ChatOptions | None = None
    ) -> AsyncIterator[ChatDelta]: ...

    @abstractmethod
    async def embeddings(self, texts: list[str], model: str | None = None) -> list[list[float]]: ...


def provider_output_token_parameter(
    provider: Any,
) -> Literal["max_tokens", "max_completion_tokens"]:
    value = getattr(provider, "output_token_parameter", "max_tokens")
    return value if value in ("max_tokens", "max_completion_tokens") else "max_tokens"


def admit_provider_payload(
    provider: Any,
    messages: list[dict[str, Any]],
    options: ChatOptions | None = None,
) -> ChatOptions:
    """Admit an exact provider payload and clamp its requested output."""
    from app.services.chat_service import _estimate_message_tokens, _estimate_tokens

    caps = getattr(provider, "capabilities", None)
    if not isinstance(caps, ModelCapabilities):
        caps = UNKNOWN_MODEL_CAPABILITIES
    opts = options or ChatOptions(
        output_token_parameter=provider_output_token_parameter(provider)
    )
    if _PROTECTED_CHAT_EXTRA_KEYS.intersection(opts.extra):
        raise PromptAdmissionError(
            INVALID_PROMPT_BUDGET,
            "Chat options cannot override protected provider payload fields",
        )
    requested_output = (
        caps.max_output_tokens if opts.max_tokens is None else opts.max_tokens
    )
    effective_output = min(requested_output, caps.max_output_tokens)
    admitted_options = replace(opts, max_tokens=effective_output)
    model_name = getattr(provider, "model", "") or ""
    supplemental_tokens = (
        _estimate_tokens(
            json.dumps(
                {"tools": admitted_options.tools, "extra": admitted_options.extra},
                ensure_ascii=False,
                default=str,
            ),
            model_name,
        )
        if admitted_options.tools or admitted_options.extra
        else 0
    )
    budget = calculate_prompt_budget(
        caps,
        requested_output=effective_output,
        tool_schema_tokens=supplemental_tokens,
    )
    message_tokens = sum(
        _estimate_message_tokens(message, model_name) for message in messages
    )
    if message_tokens > budget.input_tokens:
        raise PromptAdmissionError(
            PROMPT_TOO_LARGE,
            "The final provider payload exceeds the configured prompt budget",
        )
    return admitted_options
