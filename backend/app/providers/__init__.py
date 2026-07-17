"""Model provider layer.

- base.py: ModelProvider abstract base + DTOs (ChatOptions, ChatResult, ...).
- openai_compatible.py: OpenAICompatibleProvider over httpx (+tenacity retry).
- mock.py: MockProvider for offline / test runs.
- registry.py: get_provider_for_config(cfg) - the only entry point callers use.
"""
from app.providers.base import (
    ChatDelta,
    ChatOptions,
    ChatResult,
    ModelProvider,
    ProviderError,
    ToolCallDef,
)
from app.providers.mock import MockProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.registry import get_provider_for_config

__all__ = [
    "ModelProvider",
    "OpenAICompatibleProvider",
    "MockProvider",
    "get_provider_for_config",
    "ChatOptions",
    "ChatDelta",
    "ChatResult",
    "ToolCallDef",
    "ProviderError",
]
