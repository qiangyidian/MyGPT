"""Typed message parts + multimodal capability routing (Task 10).

A multimodal request is a list of typed parts (``TextPart`` / ``ImagePart`` /
``AudioPart`` / ``FilePart``). :func:`route_multimodal` validates those parts
against a model's :class:`~app.model_capabilities.ModelCapabilities` and raises
:class:`ModelCapabilityError` (code ``unsupported_modality``) when a part
requires a modality the model has not declared. The OpenAI-compatible provider's
transcription / speech / image-generation methods re-check the same flags before
dispatch (defense-in-depth: a misconfigured caller can never send audio to a
text-only endpoint).

Design notes:
  * ``TextPart`` / ``FilePart`` are accepted on any model (text is universal;
    a file is opaque metadata the model references, never raw bytes for a
    modality it can't handle).
  * ``ImagePart`` requires ``supports_vision``.
  * ``AudioPart`` (input) requires ``supports_audio_input``.
  * Audio *output* (speech) and *image generation* are request-side operations,
    not input message parts; they are gated at the provider method level.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.model_capabilities import ModelCapabilities


class ModelCapabilityError(Exception):
    """A requested modality is not supported by the target model.

    ``code`` is the stable machine string (``unsupported_modality``) the API
    envelope and runtime surface; ``modality`` names which modality was missing
    so an upstream can render a precise error.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "unsupported_modality",
        modality: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.modality = modality


@dataclass
class TextPart:
    """A plain text segment — accepted on every model."""

    text: str
    type: str = field(default="text", init=False)


@dataclass
class ImagePart:
    """An image input part. Requires ``supports_vision``.

    ``data_url`` is a base64 data URL (``data:image/png;base64,...``) or an
    opaque artifact reference the backend resolves with authorization.
    """

    data_url: str
    media_type: str = "image/png"
    type: str = field(default="image", init=False)


@dataclass
class AudioPart:
    """An audio input part. Requires ``supports_audio_input``."""

    data_url: str
    media_type: str = "audio/wav"
    type: str = field(default="audio", init=False)


@dataclass
class FilePart:
    """An opaque file reference (filename + media_type + size).

    Always accepted: the file is carried as referenceable context, never as raw
    bytes for a modality the model can't handle.
    """

    filename: str
    media_type: str = "application/octet-stream"
    size: int = 0
    type: str = field(default="file", init=False)


Part = Any  # TextPart | ImagePart | AudioPart | FilePart


def route_multimodal(
    parts: list[Any], capabilities: ModelCapabilities
) -> list[Any]:
    """Validate ``parts`` against ``capabilities``; return them on success.

    Raises :class:`ModelCapabilityError` (code ``unsupported_modality``) on the
    first part whose required modality the model does not declare. The check is
    performed before any provider dispatch so an unsupported request never
    reaches the network.
    """
    if capabilities is None:
        raise ModelCapabilityError("model capabilities are unknown", modality="unknown")
    for part in parts or []:
        if isinstance(part, ImagePart):
            if not capabilities.supports_vision:
                raise ModelCapabilityError(
                    "model does not support image (vision) input",
                    modality="image",
                )
        elif isinstance(part, AudioPart):
            if not capabilities.supports_audio_input:
                raise ModelCapabilityError(
                    "model does not support audio input",
                    modality="audio",
                )
        # TextPart / FilePart / unknown shapes are accepted — a file is opaque
        # metadata; text is universal.
    return list(parts or [])


__all__ = [
    "AudioPart",
    "FilePart",
    "ImagePart",
    "ModelCapabilityError",
    "TextPart",
    "route_multimodal",
]
