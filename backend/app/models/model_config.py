from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, false
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class ModelConfig(Base, TimestampMixin):
    """A configured model endpoint. user_id NULL => system-wide (shared) config."""
    __tablename__ = "model_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)  # openai-compatible | mock
    api_base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    supports_stream: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supports_tools: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_parallel_tools: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    # Vision (multimodal): when True, image attachments are sent to the model as
    # OpenAI image_url content parts. Defaults to False; auto-detected from the
    # model name heuristically at create time (see VISION_MODEL_KEYWORDS).
    supports_vision: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_audio_input: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    supports_audio_output: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    supports_image_generation: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    supports_structured_output: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    supports_reasoning_effort: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    output_token_parameter: Mapped[str] = mapped_column(
        String(32), default="max_tokens", server_default="max_tokens", nullable=False
    )
    max_context_tokens: Mapped[int] = mapped_column(Integer, default=8192, nullable=False)
    max_tokens: Mapped[int] = mapped_column(Integer, default=1024, nullable=False)
    temperature: Mapped[float] = mapped_column(Float, default=0.7, nullable=False)
    top_p: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    is_embedding: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # mark embedders

    @property
    def capabilities(self):
        from app.model_capabilities import capabilities_from_config

        return capabilities_from_config(self)
