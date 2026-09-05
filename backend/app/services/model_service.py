"""Model-config CRUD + connectivity test.

A ``ModelConfig`` row stores its API key encrypted (``api_key_encrypted``).
This service is the single place that:

  * encrypts incoming keys on create/update (``encrypt_secret``),
  * keeps the existing key when an update omits ``api_key`` (so partial edits
    don't wipe credentials),
  * exposes a masked view (``to_out``) for the API layer, and
  * performs a live connectivity ``test`` against the configured provider.
"""
from __future__ import annotations

import time
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret, encrypt_secret, mask_secret
from app.models import ModelConfig
from app.providers import ChatOptions
from app.providers.base import ProviderError
from app.providers.registry import get_provider_for_config
from app.schemas import (
    ModelConfigCreate,
    ModelConfigOut,
    ModelConfigUpdate,
    ModelTestResult,
)


async def list_for_user(
    db: AsyncSession, user_id: uuid.UUID
) -> list[ModelConfig]:
    """Return configs owned by ``user_id`` plus system-wide (user_id NULL) ones."""
    result = await db.execute(
        select(ModelConfig)
        .where(or_(ModelConfig.user_id == user_id, ModelConfig.user_id.is_(None)))
        .order_by(ModelConfig.created_at.asc())
    )
    return list(result.scalars().all())


async def get(
    db: AsyncSession, config_id: uuid.UUID, user_id: uuid.UUID
) -> ModelConfig | None:
    """Fetch a config if it belongs to ``user_id`` or is system-wide."""
    result = await db.execute(
        select(ModelConfig).where(
            ModelConfig.id == config_id,
            or_(ModelConfig.user_id == user_id, ModelConfig.user_id.is_(None)),
        )
    )
    return result.scalar_one_or_none()


async def create(
    db: AsyncSession, user_id: uuid.UUID, data: ModelConfigCreate
) -> ModelConfig:
    """Create a config, encrypting the supplied API key."""
    cfg = ModelConfig(
        user_id=user_id,
        name=data.name,
        provider=data.provider,
        api_base_url=data.api_base_url,
        api_key_encrypted=encrypt_secret(data.api_key or ""),
        model_name=data.model_name,
        embedding_model_name=data.embedding_model_name,
        supports_stream=data.supports_stream,
        supports_tools=data.supports_tools,
        supports_parallel_tools=data.supports_parallel_tools,
        supports_vision=data.supports_vision,
        supports_audio_input=data.supports_audio_input,
        supports_audio_output=data.supports_audio_output,
        supports_image_generation=data.supports_image_generation,
        supports_structured_output=data.supports_structured_output,
        supports_reasoning_effort=data.supports_reasoning_effort,
        output_token_parameter=data.output_token_parameter,
        max_context_tokens=data.max_context_tokens,
        max_tokens=data.max_tokens,
        temperature=data.temperature,
        top_p=data.top_p,
        is_embedding=data.is_embedding,
    )
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)
    return cfg


async def update(
    db: AsyncSession,
    config_id: uuid.UUID,
    user_id: uuid.UUID,
    data: ModelConfigUpdate,
) -> ModelConfig | None:
    """Patch a config. A blank/None ``api_key`` preserves the existing key."""
    cfg = await get(db, config_id, user_id)
    if cfg is None:
        return None
    updates = data.model_dump(exclude_unset=True)
    if updates.get("supports_tools") is False:
        updates["supports_parallel_tools"] = False

    api_key = updates.pop("api_key", None)
    if api_key:  # only overwrite when a non-empty key is supplied
        cfg.api_key_encrypted = encrypt_secret(api_key)
    # If api_key is None or "" -> keep the existing encrypted value.

    for field, value in updates.items():
        setattr(cfg, field, value)

    await db.commit()
    await db.refresh(cfg)
    return cfg


async def delete(
    db: AsyncSession, config_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Delete a config. System seed configs are allowed to be deleted too."""
    cfg = await get(db, config_id, user_id)
    if cfg is None:
        return False
    await db.delete(cfg)
    await db.commit()
    return True


def to_out(cfg: ModelConfig) -> ModelConfigOut:
    """Build the API-safe (masked) view of a config."""
    decrypted = decrypt_secret(cfg.api_key_encrypted)
    return ModelConfigOut(
        id=cfg.id,
        user_id=cfg.user_id,
        name=cfg.name,
        provider=cfg.provider,
        api_base_url=cfg.api_base_url,
        api_key_masked=mask_secret(decrypted),
        has_key=bool(decrypted),
        model_name=cfg.model_name,
        embedding_model_name=cfg.embedding_model_name,
        supports_stream=cfg.supports_stream,
        supports_tools=cfg.supports_tools,
        supports_parallel_tools=cfg.supports_parallel_tools,
        supports_vision=cfg.supports_vision,
        supports_audio_input=cfg.supports_audio_input,
        supports_audio_output=cfg.supports_audio_output,
        supports_image_generation=cfg.supports_image_generation,
        supports_structured_output=cfg.supports_structured_output,
        supports_reasoning_effort=cfg.supports_reasoning_effort,
        output_token_parameter=cfg.output_token_parameter,
        max_context_tokens=cfg.max_context_tokens,
        max_tokens=cfg.max_tokens,
        temperature=cfg.temperature,
        top_p=cfg.top_p,
        is_embedding=cfg.is_embedding,
        created_at=cfg.created_at,
    )


async def test(cfg: ModelConfig) -> ModelTestResult:
    """Probe a config with a minimal chat call and report latency/sample/error.

    Uses the cross-module ``get_provider_for_config`` registry contract, which
    decrypts the stored key and selects the right provider implementation.
    """
    start = time.perf_counter()
    try:
        provider = get_provider_for_config(cfg)
        result = await provider.chat(
            messages=[{"role": "user", "content": "ping"}],
            options=ChatOptions(
                max_tokens=1,
                temperature=0.0,
                output_token_parameter=cfg.output_token_parameter,
            ),
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        sample = (result.content or "").strip()[:200] or None
        return ModelTestResult(
            ok=True,
            latency_ms=latency_ms,
            sample=sample,
            error=None,
        )
    except ProviderError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ModelTestResult(ok=False, latency_ms=latency_ms, error=str(exc) or "provider error")
    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ModelTestResult(ok=False, latency_ms=latency_ms, error=str(exc) or "unknown error")
