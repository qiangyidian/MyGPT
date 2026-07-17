"""Model provider abstraction. Every LLM/embedding call goes through a ModelProvider.

Implementations live alongside (openai_compatible.py, mock.py). Business code never
constructs raw HTTP to a model — it asks the registry for a provider.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


class ProviderError(RuntimeError):
    """Raised on transport/timeout/auth errors talking to a model endpoint."""


@dataclass
class ChatOptions:
    temperature: float = 0.7
    top_p: float = 1.0
    max_tokens: int = 1024
    tools: list[dict[str, Any]] | None = None        # OpenAI tool schemas
    tool_choice: Any = "auto"
    stop: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


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
    finish_reason: str | None = None


@dataclass
class ChatResult:
    """Non-streaming result."""
    content: str = ""
    tool_calls: list[ToolCallDef] | None = None
    finish_reason: str = "stop"
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
