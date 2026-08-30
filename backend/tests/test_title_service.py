"""Auto-titling tests — ChatGPT-style sidebar titles (title_service)."""
from __future__ import annotations

import pytest

from app.services.title_service import (
    DEFAULT_TITLE,
    clean_llm_title,
    is_default_title,
    maybe_autotitle,
    maybe_autotitle_after_answer,
    truncate_title,
)


# --------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------- #

def test_is_default_title():
    assert is_default_title(None)
    assert is_default_title("")
    assert is_default_title("   ")
    assert is_default_title("新对话")
    assert not is_default_title("量子计算调研")
    assert not is_default_title(" 新对话! ")  # anything else = titled


def test_truncate_title_collapses_and_cuts():
    assert truncate_title("  帮我\n查一下   量子计算  ") == "帮我 查一下 量子计算"
    long = "字" * 40
    assert len(truncate_title(long)) == 24


def test_clean_llm_title_strips_wrappers():
    assert clean_llm_title("「AI新闻PPT制作」") == "AI新闻PPT制作"
    assert clean_llm_title('"量子计算入门"') == "量子计算入门"
    assert clean_llm_title("**标题：OpenAI API 指南**") == "OpenAI API 指南"
    assert clean_llm_title("标题：量子计算入门科普") == "量子计算入门科普"


def test_clean_llm_title_rejects_junk():
    assert clean_llm_title("") is None
    assert clean_llm_title("   ") is None
    assert clean_llm_title("新对话") is None
    assert clean_llm_title("好") is None  # < 2 chars


def test_clean_llm_title_overlong_truncates():
    out = clean_llm_title("这" * 50)
    assert out is not None and len(out) == 30


# --------------------------------------------------------------------- #
# Orchestration (with a fake conversation + fake db)
# --------------------------------------------------------------------- #

class _FakeConversation:
    def __init__(self, title=DEFAULT_TITLE):
        self.title = title


class _FakeDB:
    committed = 0

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        pass


@pytest.mark.asyncio
async def test_maybe_autotitle_sets_fallback():
    conv = _FakeConversation()
    db = _FakeDB()
    changed = await maybe_autotitle(
        db, conv, None, first_user_message="帮我生成一个PPT介绍今天的AI新闻"
    )
    assert changed is True
    assert conv.title == "帮我生成一个PPT介绍今天的AI新闻"[:24]
    assert db.committed == 1


@pytest.mark.asyncio
async def test_maybe_autotitle_skips_user_renamed():
    conv = _FakeConversation(title="我的私人对话")
    db = _FakeDB()
    changed = await maybe_autotitle(
        db, conv, None, first_user_message="随便聊点什么"
    )
    assert changed is False
    assert conv.title == "我的私人对话"
    assert db.committed == 0


@pytest.mark.asyncio
async def test_maybe_autotitle_empty_message_noop():
    conv = _FakeConversation()
    db = _FakeDB()
    assert await maybe_autotitle(db, conv, None, first_user_message="   ") is False
    assert conv.title == DEFAULT_TITLE


@pytest.mark.asyncio
async def test_maybe_autotitle_llm_wins_when_available(monkeypatch):
    class _FakeProvider:
        async def chat(self, messages, options=None):
            class _R:
                content = "「AI新闻PPT制作」"
            return _R()

    import app.providers.registry as registry_mod
    monkeypatch.setattr(registry_mod, "get_provider_for_config", lambda cfg: _FakeProvider())

    conv = _FakeConversation()
    db = _FakeDB()
    changed = await maybe_autotitle(
        db, conv, object(),  # cfg truthy
        first_user_message="帮我生成PPT",
        assistant_prefix="好的，我已生成17页PPT……",
    )
    assert changed is True
    assert conv.title == "AI新闻PPT制作"


@pytest.mark.asyncio
async def test_refine_replaces_fallback_but_not_user_rename(monkeypatch):
    class _FakeProvider:
        async def chat(self, messages, options=None):
            class _R:
                content = "量子计算调研"
            return _R()

    import app.providers.registry as registry_mod
    monkeypatch.setattr(registry_mod, "get_provider_for_config", lambda cfg: _FakeProvider())

    # 1. fallback title (== truncate(first message)) → refined by LLM
    conv = _FakeConversation(title=truncate_title("帮我调研量子计算最新进展"))
    db = _FakeDB()
    assert await maybe_autotitle_after_answer(
        db, conv, object(),
        first_user_message="帮我调研量子计算最新进展",
        assistant_prefix="以下是调研结果……",
    ) is True
    assert conv.title == "量子计算调研"

    # 2. user renamed (title != truncation of first message) → untouched
    conv2 = _FakeConversation(title="用户自己的标题")
    assert await maybe_autotitle_after_answer(
        db, conv2, object(),
        first_user_message="帮我调研量子计算最新进展",
        assistant_prefix="……",
    ) is False
    assert conv2.title == "用户自己的标题"
