"""Provider registry.

Single entry point for turning a ModelConfig ORM row into a live
ModelProvider. Business code (chat, RAG, embeddings, tool execution) calls
get_provider_for_config(cfg) and never touches raw HTTP itself.
"""
from __future__ import annotations

from app.core.security import decrypt_secret
from app.models.model_config import ModelConfig
from app.providers.base import ModelProvider, ProviderError
from app.providers.mock import MockProvider
from app.providers.openai_compatible import OpenAICompatibleProvider


def get_provider_for_config(cfg: ModelConfig) -> ModelProvider:
    """Build the right ModelProvider for a ModelConfig row.

    Decides on `cfg.provider`, decrypts the stored API key, and wires up
    base_url + model. Raises ProviderError for unknown provider types so
    callers fail loudly instead of silently falling back.
    """
    api_key = decrypt_secret(cfg.api_key_encrypted or "")
    provider_type = (cfg.provider or "").strip().lower()

    if provider_type == "openai-compatible":
        return OpenAICompatibleProvider(
            base_url=cfg.api_base_url,
            api_key=api_key,
            model=cfg.model_name,
        )
    if provider_type == "mock":
        return MockProvider(
            base_url=cfg.api_base_url,
            api_key=api_key,
            model=cfg.model_name,
        )
    raise ProviderError(f"unknown provider type: {cfg.provider!r}")
