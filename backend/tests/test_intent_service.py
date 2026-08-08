"""IntentService: the engineering-grade model-driven intent classifier.

Covers robust parsing (fences, prose, aliases, invalid route), resilience
(retry, timeout, provider error), and the failure contract (None → fallback).
Uses a scripted provider so no real model is contacted.
"""
from __future__ import annotations

import asyncio

import pytest

from app.agents.context_fragments import IntentContextInput, assemble_context_fragments
from app.agents.intent_service import IntentClassifierConfig, IntentService
from app.providers.base import ChatResult, ProviderError
from app.providers.mock import MockProvider


def _fragments(user_content: str = "用 Python 写一个贪吃蛇游戏"):
    return assemble_context_fragments(IntentContextInput(user_content=user_content))


class _ScriptedProvider(MockProvider):
    """Returns/raises a scripted sequence of chat() outcomes (repeats the last)."""

    def __init__(self, *, script=None, delay: float = 0.0):
        super().__init__(base_url="http://x/v1", model="mock")
        self._script = list(script or [])
        self._delay = delay
        self.calls = 0

    async def chat(self, messages, options=None):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if not self._script:
            return ChatResult(content="", finish_reason="stop")
        idx = min(self.calls - 1, len(self._script) - 1)
        item = self._script[idx]
        if isinstance(item, BaseException):
            raise item
        return ChatResult(content=item, finish_reason="stop")


def _service(**kw) -> IntentService:
    """Build a service with a no-retry, fast-timeout config for deterministic tests."""
    cfg = IntentClassifierConfig(max_retries=0, timeout_seconds=2.0)
    return IntentService(cfg)


async def test_judge_parses_plain_json():
    p = _ScriptedProvider(script=['{"route":"native","deliverable_kind":"code","confidence":0.9,"rationale":"代码"}'])
    d = await _service().judge(user_content="写贪吃蛇", fragments=_fragments(), provider=p)
    assert d is not None and d.route == "native" and d.deliverable_kind == "code"
    assert d.confidence == pytest.approx(0.9)


async def test_judge_strips_markdown_fence():
    p = _ScriptedProvider(script=['```json\n{"route":"debate","deliverable_kind":"factual","confidence":0.8}\n```'])
    d = await _service().judge(user_content="React vs Vue 哪个好", fragments=_fragments(), provider=p)
    assert d is not None and d.route == "debate"


async def test_judge_extracts_json_from_surrounding_prose():
    p = _ScriptedProvider(script=['好的，分析如下：\n{"route":"deep_research","deliverable_kind":"factual","confidence":0.85}\n以上。'])
    d = await _service().judge(user_content="深入调研大模型微调", fragments=_fragments(), provider=p)
    assert d is not None and d.route == "deep_research"


async def test_judge_coerces_route_alias():
    # Model said "research" instead of "deep_research" -> coerced.
    p = _ScriptedProvider(script=['{"route":"research","deliverable_kind":"qa","confidence":0.7}'])
    d = await _service().judge(user_content="调研一下", fragments=_fragments(), provider=p)
    assert d is not None and d.route == "deep_research"
    assert d.deliverable_kind == "factual"  # "qa" alias coerced


async def test_judge_rejects_invalid_route():
    # An unknown route must NOT silently mis-route the turn -> None (fallback).
    p = _ScriptedProvider(script=['{"route":"teleport","deliverable_kind":"code","confidence":0.99}'])
    d = await _service().judge(user_content="x", fragments=_fragments(), provider=p)
    assert d is None


async def test_judge_clamps_confidence_and_defaults_kind():
    p = _ScriptedProvider(script=['{"route":"native","confidence":42.0}'])
    d = await _service().judge(user_content="x", fragments=_fragments(), provider=p)
    assert d is not None
    assert d.confidence == 1.0  # clamped
    assert d.deliverable_kind == "factual"  # missing -> default


async def test_judge_provider_error_returns_none():
    p = _ScriptedProvider(script=[ProviderError("boom", code="provider_error")])
    d = await _service().judge(user_content="x", fragments=_fragments(), provider=p)
    assert d is None


async def test_judge_timeout_returns_none():
    cfg = IntentClassifierConfig(max_retries=0, timeout_seconds=0.1)
    p = _ScriptedProvider(delay=0.3)
    d = await IntentService(cfg).judge(user_content="x", fragments=_fragments(), provider=p)
    assert d is None


async def test_judge_retries_then_succeeds():
    # First call errors, retry returns valid JSON.
    p = _ScriptedProvider(script=[
        ProviderError("transient", code="provider_error"),
        '{"route":"native","deliverable_kind":"factual","confidence":0.8}',
    ])
    cfg = IntentClassifierConfig(max_retries=1, timeout_seconds=2.0)
    d = await IntentService(cfg).judge(user_content="x", fragments=_fragments(), provider=p)
    assert d is not None and d.route == "native"
    assert p.calls == 2  # initial + 1 retry


async def test_judge_returns_none_when_no_provider():
    d = await _service().judge(user_content="x", fragments=_fragments(), provider=None)
    assert d is None


async def test_judge_returns_none_for_empty_content():
    d = await _service().judge(user_content="   ", fragments=_fragments(), provider=_ScriptedProvider())
    assert d is None


async def test_judge_disabled_short_circuits_without_provider_call():
    # Kill switch: when disabled, judge returns None and never calls the provider.
    p = _ScriptedProvider(script=['{"route":"native","deliverable_kind":"code","confidence":0.9}'])
    cfg = IntentClassifierConfig(enabled=False, max_retries=0)
    d = await IntentService(cfg).judge(user_content="写贪吃蛇", fragments=_fragments(), provider=p)
    assert d is None
    assert p.calls == 0


async def test_judge_caches_identical_request(monkeypatch):
    # An identical second call must hit the cache (no second provider call).
    import app.agents.intent_service as svc

    monkeypatch.setattr(svc, "_cache_active", lambda: True)
    svc.clear_intent_cache()
    try:
        p = _ScriptedProvider(script=['{"route":"native","deliverable_kind":"code","confidence":0.9}'])
        srv = _service()
        d1 = await srv.judge(user_content="写贪吃蛇", fragments=_fragments(), provider=p)
        d2 = await srv.judge(user_content="写贪吃蛇", fragments=_fragments(), provider=p)
        assert d1 is not None and d2 is not None
        assert d1.route == d2.route == "native"
        assert p.calls == 1  # second call served from cache
    finally:
        svc.clear_intent_cache()
