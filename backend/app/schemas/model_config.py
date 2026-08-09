from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class ModelConfigBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    provider: str = "openai-compatible"            # openai-compatible | mock
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
    max_context_tokens: int = Field(default=8192, gt=0)
    max_tokens: int = Field(default=1024, gt=0)
    temperature: float = 0.7
    top_p: float = 1.0
    is_embedding: bool = False


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
    temperature: float | None = None
    top_p: float | None = None
    is_embedding: bool | None = None


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
