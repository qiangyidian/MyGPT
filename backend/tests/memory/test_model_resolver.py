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


@pytest.mark.asyncio
async def test_explicit_embedding_config_is_rejected(db_session, monkeypatch):
    """指到 embedding 配置上必须被拦下并退回会话模型。

    否则运维把 MEMORY_MODEL_ID 写成一个向量模型的 id 时会"静默成功"，然后在调用点以
    一个难懂的错收场（记忆维护要的是能跑 chat/completions 的模型）。
    """
    conv_cfg = await _mk_config(db_session, name="conv")
    emb = ModelConfig(
        user_id=None,
        name="emb",
        provider="openai-compatible",
        api_base_url="http://localhost/v1",
        model_name="emb-model",
        is_embedding=True,
    )
    db_session.add(emb)
    await db_session.flush()
    monkeypatch.setattr(get_settings(), "MEMORY_MODEL_ID", str(emb.id), raising=False)

    resolved = await resolve_memory_model(db_session, conversation_cfg_id=conv_cfg.id)
    assert resolved is not None and resolved.id == conv_cfg.id
