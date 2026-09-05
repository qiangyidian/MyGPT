from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.ssrf import check_url_shape
from app.schemas.common import ORMModel


class ModelConfigBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # Real providers only. "mock" is a test-only stand-in (app.providers.mock)
    # and must never be creatable through the public API — a mock row in the
    # picker gives users fake "You said: ..." replies that look like bugs.
    provider: Literal["openai-compatible", "anthropic", "hermes"] = "openai-compatible"
    # anthropic 的原生 Messages API 端点；api_base_url 可留空默认 https://api.anthropic.com，
    # 但 schema 校验要求非空 —— 创建时前端会自动填入默认值。
    api_base_url: str = Field(min_length=1, max_length=512)
    api_key: str | None = None                      # write-only; stored encrypted
    model_name: str = Field(min_length=1, max_length=128)
    embedding_model_name: str | None = None
    supports_stream: bool = True
    supports_tools: bool = False
    supports_parallel_tools: bool = False
    supports_vision: bool = False
    supports_audio_input: bool = False
    supports_audio_output: bool = False
    supports_image_generation: bool = False
    supports_structured_output: bool = False
    supports_reasoning_effort: bool = False
    output_token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    max_context_tokens: int = Field(default=131072, gt=0)
    max_tokens: int = Field(default=8192, gt=0)
    temperature: float = Field(default=0.7, ge=0, le=2, allow_inf_nan=False)
    top_p: float = Field(default=1.0, gt=0, le=1, allow_inf_nan=False)
    is_embedding: bool = False

    @field_validator("api_base_url")
    @classmethod
    def api_base_url_shape(cls, v: str) -> str:
        # SSRF shape guard at the schema boundary: http/https only, no embedded
        # credentials. (The private-address resolution check runs at the API
        # layer where the caller's role / environment is known.)
        try:
            check_url_shape(v)
        except Exception as exc:  # EndpointBlockedError is a ValueError subclass
            raise ValueError(str(exc)) from exc
        return v

    @model_validator(mode="after")
    def parallel_tools_require_tools(self) -> ModelConfigBase:
        if self.supports_parallel_tools and not self.supports_tools:
            raise ValueError("parallel tool support requires tool support")
        return self


class ModelConfigCreate(ModelConfigBase):
    pass


class ModelConfigUpdate(BaseModel):
    name: str | None = None
    api_base_url: str | None = None
    api_key: str | None = None
    model_name: str | None = None
    embedding_model_name: str | None = None
    supports_stream: bool | None = None
    supports_tools: bool | None = None
    supports_parallel_tools: bool | None = None
    supports_vision: bool | None = None
    supports_audio_input: bool | None = None
    supports_audio_output: bool | None = None
    supports_image_generation: bool | None = None
    supports_structured_output: bool | None = None
    supports_reasoning_effort: bool | None = None
    output_token_parameter: Literal["max_tokens", "max_completion_tokens"] | None = None
    max_context_tokens: int | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(
        default=None, ge=0, le=2, allow_inf_nan=False
    )
    top_p: float | None = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    is_embedding: bool | None = None

    @model_validator(mode="after")
    def reject_explicit_nulls_for_non_nullable_fields(self) -> ModelConfigUpdate:
        nullable_updates = {"api_key", "embedding_model_name"}
        explicit_nulls = [
            field
            for field in self.model_fields_set
            if field not in nullable_updates and getattr(self, field) is None
        ]
        if explicit_nulls:
            raise ValueError(
                "fields cannot be null: " + ", ".join(sorted(explicit_nulls))
            )
        if self.supports_parallel_tools is True and self.supports_tools is not True:
            raise ValueError(
                "supports_parallel_tools requires supports_tools=true in the same update"
            )
        return self


class ModelConfigOut(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    name: str
    provider: str
    api_base_url: str
    api_key_masked: str = ""        # never the raw key
    has_key: bool = False
    model_name: str
    embedding_model_name: str | None
    supports_stream: bool
    supports_tools: bool
    supports_parallel_tools: bool
    supports_vision: bool
    supports_audio_input: bool
    supports_audio_output: bool
    supports_image_generation: bool
    supports_structured_output: bool
    supports_reasoning_effort: bool
    output_token_parameter: Literal["max_tokens", "max_completion_tokens"]
    max_context_tokens: int
    max_tokens: int
    temperature: float
    top_p: float
    is_embedding: bool
    created_at: datetime


class ModelTestResult(BaseModel):
    ok: bool
    latency_ms: int
    sample: str | None = None
    error: str | None = None
