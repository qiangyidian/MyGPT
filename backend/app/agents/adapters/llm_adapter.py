"""Build a CrewAI ``LLM`` from an app :class:`ModelConfig`.

CrewAI uses LiteLLM under the hood. For OpenAI-compatible endpoints we prefix
the model name with ``openai/`` and pass ``base_url`` + decrypted ``api_key``.
The API key never leaves the existing encrypted ``ModelConfig`` — CrewAI does
not read browser input or a separate secret store.
"""
from __future__ import annotations

from typing import Any

from app.core.security import decrypt_secret
from app.models import ModelConfig


class CrewAILLMFactory:
    """Turn a ModelConfig row into a CrewAI LLM instance."""

    @staticmethod
    def from_model_config(cfg: ModelConfig) -> Any:
        from crewai import LLM  # lazy: crewai is optional

        api_key = decrypt_secret(cfg.api_key_encrypted or "") or "dummy"

        # LiteLLM convention: openai-compatible providers use the "openai/" prefix.
        model_name = cfg.model_name
        if "/" not in model_name:
            model_name = f"openai/{model_name}"

        kwargs: dict[str, Any] = dict(
            model=model_name,
            base_url=cfg.api_base_url,
            api_key=api_key,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
        )
        if cfg.top_p is not None:
            kwargs["top_p"] = cfg.top_p

        return LLM(**kwargs)
