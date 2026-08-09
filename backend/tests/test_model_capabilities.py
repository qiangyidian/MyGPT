from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest
from pydantic import ValidationError

from app.model_capabilities import ModelCapabilities, capabilities_from_config
from app.providers.base import ChatOptions
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.schemas.model_config import ModelConfigCreate, ModelConfigUpdate
from app.services.model_service import to_out


def test_model_capabilities_are_immutable_and_require_positive_limits():
    caps = ModelCapabilities(context_window=8192, max_output_tokens=1024)

    with pytest.raises(FrozenInstanceError):
        caps.context_window = 4096  # type: ignore[misc]

    with pytest.raises(ValueError):
        ModelCapabilities(context_window=0, max_output_tokens=1024)
    with pytest.raises(ValueError):
        ModelCapabilities(context_window=8192, max_output_tokens=-1)


def test_capability_conversion_uses_legacy_limits_and_conservative_defaults():
    cfg = SimpleNamespace(max_context_tokens=32768, max_tokens=2048)

    caps = capabilities_from_config(cfg)

    assert caps.context_window == 32768
    assert caps.max_output_tokens == 2048
    assert caps.supports_tools is False
    assert caps.supports_parallel_tools is False
    assert caps.supports_vision is False
    assert caps.supports_audio_input is False
    assert caps.supports_audio_output is False
    assert caps.supports_image_generation is False
    assert caps.supports_structured_output is False
    assert caps.supports_reasoning_effort is False
    assert caps.output_token_parameter == "max_tokens"


def test_capability_conversion_reads_additive_fields():
    cfg = SimpleNamespace(
        max_context_tokens=128000,
        max_tokens=8192,
        supports_tools=True,
        supports_parallel_tools=True,
        supports_vision=True,
        supports_audio_input=True,
        supports_audio_output=True,
        supports_image_generation=True,
        supports_structured_output=True,
        supports_reasoning_effort=True,
        output_token_parameter="max_completion_tokens",
    )

    caps = capabilities_from_config(cfg)

    assert caps.supports_parallel_tools is True
    assert caps.supports_audio_input is True
    assert caps.supports_audio_output is True
    assert caps.supports_image_generation is True
    assert caps.supports_structured_output is True
    assert caps.supports_reasoning_effort is True
    assert caps.output_token_parameter == "max_completion_tokens"


def test_model_config_schemas_validate_limits_and_output_parameter():
    common = {
        "name": "Reasoning model",
        "api_base_url": "https://example.test/v1",
        "model_name": "reasoner",
    }
    created = ModelConfigCreate(
        **common,
        max_context_tokens=32768,
        max_tokens=2048,
        supports_reasoning_effort=True,
        output_token_parameter="max_completion_tokens",
    )
    assert created.max_context_tokens == 32768
    assert created.output_token_parameter == "max_completion_tokens"

    with pytest.raises(ValidationError):
        ModelConfigCreate(**common, max_context_tokens=0)
    with pytest.raises(ValidationError):
        ModelConfigUpdate(max_tokens=-1)
    with pytest.raises(ValidationError):
        ModelConfigUpdate(output_token_parameter="both")


def test_provider_maps_generic_output_limit_to_selected_parameter():
    payload = OpenAICompatibleProvider._build_chat_payload(
        "reasoner",
        [{"role": "user", "content": "hello"}],
        ChatOptions(max_tokens=500, output_token_parameter="max_completion_tokens"),
        stream=False,
    )

    assert payload["max_completion_tokens"] == 500
    assert "max_tokens" not in payload


def test_chat_options_reject_unknown_output_parameter():
    with pytest.raises(ValueError):
        ChatOptions(output_token_parameter="unsupported")  # type: ignore[arg-type]


def test_provider_never_emits_both_output_parameters_from_extra():
    payload = OpenAICompatibleProvider._build_chat_payload(
        "reasoner",
        [{"role": "user", "content": "hello"}],
        ChatOptions(
            max_tokens=500,
            output_token_parameter="max_completion_tokens",
            extra={"max_tokens": 999},
        ),
        stream=False,
    )

    assert payload["max_completion_tokens"] == 500
    assert "max_tokens" not in payload


def test_model_service_output_exposes_capability_fields():
    cfg = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="Reasoner",
        provider="openai-compatible",
        api_base_url="https://example.test/v1",
        api_key_encrypted="",
        model_name="reasoner",
        embedding_model_name=None,
        supports_stream=True,
        supports_tools=True,
        supports_parallel_tools=True,
        supports_vision=False,
        supports_audio_input=False,
        supports_audio_output=False,
        supports_image_generation=False,
        supports_structured_output=True,
        supports_reasoning_effort=True,
        output_token_parameter="max_completion_tokens",
        max_context_tokens=32768,
        max_tokens=2048,
        temperature=0.7,
        top_p=1.0,
        is_embedding=False,
        created_at=datetime.now(timezone.utc),
    )

    out = to_out(cfg)

    assert out.supports_parallel_tools is True
    assert out.supports_structured_output is True
    assert out.supports_reasoning_effort is True
    assert out.output_token_parameter == "max_completion_tokens"
