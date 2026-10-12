"""记忆侧模型解析（spec §11「计费」段）。"""
from __future__ import annotations

import uuid

import pytest

from app.agents.memory.model_resolver import resolve_memory_model
from app.core.config import get_settings
from app.models import ModelConfig


async def _mk_config(db, *, name: str, owner=None) -> ModelConfig:
    cfg = ModelConfig(
        user_id=owner, name=name, provider="openai-compatible",
        api_base_url="http://localhost/v1", model_name=f"{name}-model",
        is_embedding=False,
    )
    db.add(cfg)
    await db.flush()
    return cfg


@pytest.mark.asyncio
async def test_explicit_memory_model_wins(db_session, monkeypatch):
    cheap = await _mk_config(db_session, name="cheap")
    monkeypatch.setattr(get_settings(), "MEMORY_MODEL_ID", str(cheap.id), raising=False)
    resolved = await resolve_memory_model(db_session, conversation_cfg_id=None)
    assert resolved is not None and resolved.id == cheap.id


@pytest.mark.asyncio
async def test_falls_back_to_conversation_model(db_session, monkeypatch):
    conv_cfg = await _mk_config(db_session, name="conv")
    monkeypatch.setattr(get_settings(), "MEMORY_MODEL_ID", "", raising=False)
    resolved = await resolve_memory_model(db_session, conversation_cfg_id=conv_cfg.id)
    assert resolved is not None and resolved.id == conv_cfg.id


@pytest.mark.asyncio
async def test_explicit_id_wins_over_conversation_model(db_session, monkeypatch):
    conv_cfg = await _mk_config(db_session, name="conv")
    cheap = await _mk_config(db_session, name="cheap")
    monkeypatch.setattr(get_settings(), "MEMORY_MODEL_ID", str(cheap.id), raising=False)
    resolved = await resolve_memory_model(db_session, conversation_cfg_id=conv_cfg.id)
    assert resolved is not None and resolved.id == cheap.id


@pytest.mark.asyncio
async def test_unknown_explicit_id_falls_back_to_conversation(db_session, monkeypatch):
    conv_cfg = await _mk_config(db_session, name="conv")
    monkeypatch.setattr(get_settings(), "MEMORY_MODEL_ID", str(uuid.uuid4()), raising=False)
    resolved = await resolve_memory_model(db_session, conversation_cfg_id=conv_cfg.id)
    assert resolved is not None and resolved.id == conv_cfg.id


@pytest.mark.asyncio
async def test_nothing_configured_returns_none(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "MEMORY_MODEL_ID", "", raising=False)
    assert await resolve_memory_model(db_session, conversation_cfg_id=None) is None
