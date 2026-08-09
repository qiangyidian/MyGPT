"""Model-config router: CRUD for configured model endpoints.

Ownership: a user sees/edits their own configs plus system-wide (user_id IS NULL)
configs. Admins see/edit all. API keys are write-only — never returned, only masked.
"""
from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import decrypt_secret, encrypt_secret, mask_secret
from app.db import get_db
from app.models import ModelConfig, User
from app.schemas import ModelConfigCreate, ModelConfigOut, ModelConfigUpdate, ModelTestResult
from app.core.deps import get_current_user

router = APIRouter(prefix="/api/models", tags=["models"])

NOT_FOUND = status.HTTP_404_NOT_FOUND
FORBID = status.HTTP_403_FORBIDDEN


def _looks_like_vision_model(model_name: str) -> bool:
    """Heuristic: does ``model_name`` look vision-capable (per VISION_MODEL_KEYWORDS)?"""
    kws = (get_settings().VISION_MODEL_KEYWORDS or "").lower().split(",")
    name = (model_name or "").lower()
    return any(k.strip() and k.strip() in name for k in kws)


def _to_out(cfg: ModelConfig) -> ModelConfigOut:
    raw_key = decrypt_secret(cfg.api_key_encrypted) if cfg.api_key_encrypted else ""
    return ModelConfigOut(
        id=cfg.id,
        user_id=cfg.user_id,
        name=cfg.name,
        provider=cfg.provider,
        api_base_url=cfg.api_base_url,
        api_key_masked=mask_secret(raw_key),
        has_key=bool(raw_key),
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


async def _load_owned_or_shared(
    db: AsyncSession, cfg_id: uuid.UUID, user: User
) -> ModelConfig:
    cfg = await db.get(ModelConfig, cfg_id)
    if cfg is None:
        raise HTTPException(NOT_FOUND, "Model config not found")
    if cfg.user_id is None:
        return cfg  # system-wide, visible to everyone
    if cfg.user_id != user.id and user.role != "admin":
        raise HTTPException(NOT_FOUND, "Model config not found")  # 404, not 403 (ownership hide)
    return cfg


@router.get("", response_model=list[ModelConfigOut])
async def list_models(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ModelConfigOut]:
    """Own configs + system-wide configs (admins see everything)."""
    if user.role == "admin":
        stmt = select(ModelConfig).order_by(ModelConfig.created_at.desc())
    else:
        stmt = (
            select(ModelConfig)
            .where(or_(ModelConfig.user_id == user.id, ModelConfig.user_id.is_(None)))
            .order_by(ModelConfig.created_at.desc())
        )
    res = await db.execute(stmt)
    return [_to_out(c) for c in res.scalars().all()]


@router.post("", response_model=ModelConfigOut, status_code=status.HTTP_201_CREATED)
async def create_model(
    payload: ModelConfigCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ModelConfigOut:
    cfg = ModelConfig(
        user_id=user.id,
        name=payload.name,
        provider=payload.provider,
        api_base_url=payload.api_base_url,
        api_key_encrypted=encrypt_secret(payload.api_key) if payload.api_key else "",
        model_name=payload.model_name,
        embedding_model_name=payload.embedding_model_name,
        supports_stream=payload.supports_stream,
        supports_tools=payload.supports_tools,
        supports_parallel_tools=payload.supports_parallel_tools,
        supports_vision=payload.supports_vision or _looks_like_vision_model(payload.model_name),
        supports_audio_input=payload.supports_audio_input,
        supports_audio_output=payload.supports_audio_output,
        supports_image_generation=payload.supports_image_generation,
        supports_structured_output=payload.supports_structured_output,
        supports_reasoning_effort=payload.supports_reasoning_effort,
        output_token_parameter=payload.output_token_parameter,
        max_context_tokens=payload.max_context_tokens,
        max_tokens=payload.max_tokens,
        temperature=payload.temperature,
        top_p=payload.top_p,
        is_embedding=payload.is_embedding,
    )
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)
    return _to_out(cfg)


@router.put("/{cfg_id}", response_model=ModelConfigOut)
async def update_model(
    cfg_id: uuid.UUID,
    payload: ModelConfigUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ModelConfigOut:
    cfg = await _load_owned_or_shared(db, cfg_id, user)
    # System-wide configs are admin-only to mutate.
    if cfg.user_id is None and user.role != "admin":
        raise HTTPException(FORBID, "Only admins can edit system-wide configs")

    data = payload.model_dump(exclude_unset=True)
    # api_key is write-only and never echoed; empty string means "leave unchanged".
    if "api_key" in data:
        new_key = data.pop("api_key")
        if new_key:
            cfg.api_key_encrypted = encrypt_secret(new_key)
    for field, value in data.items():
        setattr(cfg, field, value)

    # If the model name changed and vision wasn't explicitly set this request,
    # re-run the heuristic so switching to e.g. a Qwen-VL model auto-enables it.
    if "model_name" in data and "supports_vision" not in data:
        cfg.supports_vision = cfg.supports_vision or _looks_like_vision_model(cfg.model_name)

    await db.commit()
    await db.refresh(cfg)
    return _to_out(cfg)


@router.delete("/{cfg_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model(
    cfg_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    cfg = await _load_owned_or_shared(db, cfg_id, user)
    if cfg.user_id is None and user.role != "admin":
        raise HTTPException(FORBID, "Only admins can delete system-wide configs")
    await db.delete(cfg)
    await db.commit()


@router.post("/{cfg_id}/test", response_model=ModelTestResult)
async def test_model(
    cfg_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ModelTestResult:
    """Smoke-test a config by issuing a tiny chat completion against the provider."""
    cfg = await _load_owned_or_shared(db, cfg_id, user)

    # Imported lazily so a missing contract at import time never blocks the router.
    from app.providers.registry import get_provider_for_config
    from app.providers.base import ChatOptions, ProviderError

    start = time.perf_counter()
    try:
        provider = get_provider_for_config(cfg)
        result = await provider.chat(
            [{"role": "user", "content": "ping"}],
            ChatOptions(
                max_tokens=8,
                temperature=0.0,
                output_token_parameter=cfg.output_token_parameter,
            ),
        )
        latency = int((time.perf_counter() - start) * 1000)
        return ModelTestResult(ok=True, latency_ms=latency, sample=(result.content or "")[:200])
    except ProviderError as exc:
        latency = int((time.perf_counter() - start) * 1000)
        return ModelTestResult(ok=False, latency_ms=latency, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface any failure to the caller
        latency = int((time.perf_counter() - start) * 1000)
        return ModelTestResult(ok=False, latency_ms=latency, error=str(exc))
