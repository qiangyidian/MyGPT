"""Task 10: typed message parts + multimodal capability routing.

``route_multimodal`` validates a list of typed parts (text/image/audio/file)
against a model's :class:`ModelCapabilities` and raises
:class:`ModelCapabilityError` (code ``unsupported_modality``) when a part needs
a modality the model does not declare. The provider's transcription / speech /
image-generation routes check the same flags before dispatch (defense-in-depth).
"""
from __future__ import annotations

import pytest

from app.model_capabilities import ModelCapabilities
from app.providers.multimodal import (
    AudioPart,
    FilePart,
    ImagePart,
    ModelCapabilityError,
    TextPart,
    route_multimodal,
)


def _caps(**flags) -> ModelCapabilities:
    return ModelCapabilities(context_window=8192, max_output_tokens=1024, **flags)


TEXT_ONLY = _caps()
VISION = _caps(supports_vision=True)
AUDIO_IN = _caps(supports_audio_input=True)
AUDIO_OUT = _caps(supports_audio_output=True)
IMAGE_GEN = _caps(supports_image_generation=True)


# ---------------------------------------------------------------------------
# Text — accepted on every model.
# ---------------------------------------------------------------------------
def test_text_part_accepted_on_text_only_model():
    parts = route_multimodal([TextPart(text="hello")], TEXT_ONLY)
    assert len(parts) == 1
    assert isinstance(parts[0], TextPart)


def test_mixed_text_and_image_accepted_on_vision_model():
    parts = route_multimodal(
        [TextPart(text="describe"), ImagePart(data_url="data:image/png;base64,AAA", media_type="image/png")],
        VISION,
    )
    assert len(parts) == 2


# ---------------------------------------------------------------------------
# Image parts → require supports_vision.
# ---------------------------------------------------------------------------
def test_image_part_rejected_on_text_only_model():
    with pytest.raises(ModelCapabilityError) as exc:
        route_multimodal(
            [ImagePart(data_url="data:image/png;base64,AAA", media_type="image/png")],
            TEXT_ONLY,
        )
    assert exc.value.code == "unsupported_modality"


def test_image_part_accepted_on_vision_model():
    parts = route_multimodal(
        [ImagePart(data_url="data:image/png;base64,AAA", media_type="image/png")],
        VISION,
    )
    assert len(parts) == 1


# ---------------------------------------------------------------------------
# Audio input parts → require supports_audio_input.
# ---------------------------------------------------------------------------
def test_audio_request_rejects_text_only_model():
    with pytest.raises(ModelCapabilityError):
        route_multimodal(
            [AudioPart(data_url="data:audio/wav;base64,AAA", media_type="audio/wav")],
            TEXT_ONLY,
        )


def test_audio_part_accepted_on_audio_input_model():
    parts = route_multimodal(
        [AudioPart(data_url="data:audio/wav;base64,AAA", media_type="audio/wav")],
        AUDIO_IN,
    )
    assert len(parts) == 1


def test_capability_error_names_the_missing_modality():
    with pytest.raises(ModelCapabilityError) as exc:
        route_multimodal(
            [AudioPart(data_url="data:audio/wav;base64,AAA", media_type="audio/wav")],
            TEXT_ONLY,
        )
    assert exc.value.code == "unsupported_modality"
    # The error must say which modality is missing so callers can surface it.
    assert "audio" in exc.value.message.lower()


# ---------------------------------------------------------------------------
# File parts — always accepted (a file is carried as opaque context, the model
# never gets raw bytes for an unsupported modality).
# ---------------------------------------------------------------------------
def test_file_part_accepted_on_text_only_model():
    parts = route_multimodal(
        [FilePart(filename="report.pdf", media_type="application/pdf", size=12345)],
        TEXT_ONLY,
    )
    assert len(parts) == 1


def test_unknown_part_type_rejected():
    """Defense-in-depth: an unrecognized part type must not slip past the gate."""
    class VideoPart:
        type = "video"

    with pytest.raises(ModelCapabilityError) as exc:
        route_multimodal([VideoPart()], VISION)  # type: ignore[list-item]
    assert exc.value.code == "unsupported_modality"
    assert exc.value.modality == "unknown"


# ---------------------------------------------------------------------------
# Provider-side multimodal routes: capability-gated BEFORE any HTTP dispatch.
# Defense-in-depth: even if route_multimodal let a part through, the provider
# method re-checks the capability and refuses to call the endpoint.
# ---------------------------------------------------------------------------
def test_transcribe_refused_without_audio_input_capability():
    from app.providers.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        base_url="http://localhost:1111", api_key="x", model="text-only",
        capabilities=TEXT_ONLY,
    )
    with pytest.raises(ModelCapabilityError):
        # Must raise before any network call (no server is listening).
        import asyncio

        asyncio.run(provider.transcribe(b"\x00\x01\x02", mime_type="audio/wav"))


def test_speak_refused_without_audio_output_capability():
    from app.providers.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        base_url="http://localhost:1111", api_key="x", model="text-only",
        capabilities=TEXT_ONLY,
    )
    with pytest.raises(ModelCapabilityError):
        import asyncio

        asyncio.run(provider.speak("hi"))


def test_generate_image_refused_without_image_generation_capability():
    from app.providers.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        base_url="http://localhost:1111", api_key="x", model="text-only",
        capabilities=TEXT_ONLY,
    )
    with pytest.raises(ModelCapabilityError):
        import asyncio

        asyncio.run(provider.generate_image("a cat"))
