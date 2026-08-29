"""Provider registry.

Single entry point for turning a ModelConfig ORM row into a live
ModelProvider. Business code (chat, RAG, embeddings, tool execution) calls
get_provider_for_config(cfg) and never touches raw HTTP itself.
"""
from __future__ import annotations

from app.core.security import decrypt_secret
from app.models.model_config import ModelConfig
from app.model_capabilities import capabilities_from_config
from app.providers.anthropic import AnthropicProvider
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
    capabilities = capabilities_from_config(cfg)

    if provider_type == "openai-compatible":
        return OpenAICompatibleProvider(
            base_url=cfg.api_base_url,
            api_key=api_key,
            model=cfg.model_name,
            output_token_parameter=getattr(cfg, "output_token_parameter", "max_tokens"),
            capabilities=capabilities,
        )
    if provider_type == "mock":
        return MockProvider(
            base_url=cfg.api_base_url,
            api_key=api_key,
            model=cfg.model_name,
            output_token_parameter=getattr(cfg, "output_token_parameter", "max_tokens"),
            capabilities=capabilities,
        )
    if provider_type == "anthropic":
        # Native Messages API (/v1/messages). base_url may be empty — the
        # provider defaults to https://api.anthropic.com. Anthropic has no
        # embeddings endpoint; embedding configs must use openai-compatible.
        return AnthropicProvider(
            base_url=cfg.api_base_url,
            api_key=api_key,
            model=cfg.model_name,
            capabilities=capabilities,
        )
    raise ProviderError(f"unknown provider type: {cfg.provider!r}")
