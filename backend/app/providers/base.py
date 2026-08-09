"""Model provider abstraction. Every LLM/embedding call goes through a ModelProvider.

Implementations live alongside (openai_compatible.py, mock.py). Business code never
constructs raw HTTP to a model — it asks the registry for a provider.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal


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
    # None = omit max_tokens from the request so the endpoint uses its own maximum
    # (no output truncation). The multi-agent streaming Writer uses this so long
    # code answers are never cut off at finish_reason=length.
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

    def __init__(self, *, base_url: str, api_key: str = "", model: str = "", **_: Any) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    @abstractmethod
    async def chat(self, messages: list[dict[str, Any]], options: ChatOptions | None = None) -> ChatResult: ...

    @abstractmethod
    def stream_chat(
        self, messages: list[dict[str, Any]], options: ChatOptions | None = None
    ) -> AsyncIterator[ChatDelta]: ...

    @abstractmethod
    async def embeddings(self, texts: list[str], model: str | None = None) -> list[list[float]]: ...
