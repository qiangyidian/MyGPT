"""Guardian: fail-closed LLM judge + rejection circuit breaker."""
from __future__ import annotations

import asyncio

from app.agents.guardian import (
    GuardianService,
    GuardianVerdict,
    RejectionCircuitBreaker,
)
from app.providers.base import ChatResult, ProviderError
from app.providers.mock import MockProvider


class _ScriptedProvider(MockProvider):
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


def _action(**kw):
    return {"tool": "shell", "argv": ["rm"], **kw}


async def test_guardian_allows_low_risk():
    p = _ScriptedProvider(script=['{"risk_level":"low","user_authorization":"high","outcome":"allow","rationale":"read only"}'])
    v = await GuardianService().judge(action=_action(), provider=p)
    assert v.allowed and v.risk_level == "low"


async def test_guardian_denies_high_risk_even_when_authorized():
    p = _ScriptedProvider(script=['{"risk_level":"critical","user_authorization":"high","outcome":"deny","rationale":"exfil"}'])
    v = await GuardianService().judge(action=_action(), provider=p)
    assert not v.allowed


async def test_guardian_fail_closed_on_garbage():
    p = _ScriptedProvider(script=["this is not json at all"])
    v = await GuardianService().judge(action=_action(), provider=p)
    assert not v.allowed and v.risk_level == "high"


async def test_guardian_fail_closed_on_ambiguous_outcome():
    p = _ScriptedProvider(script=['{"risk_level":"low","outcome":"maybe"}'])
    v = await GuardianService().judge(action=_action(), provider=p)
    assert not v.allowed  # unknown outcome -> deny


async def test_guardian_fail_closed_on_provider_error():
    p = _ScriptedProvider(script=[ProviderError("boom", code="provider_error")])
    v = await GuardianService().judge(action=_action(), provider=p)
    assert not v.allowed


async def test_guardian_fail_closed_on_timeout():
    from app.agents.guardian import GuardianConfig
    p = _ScriptedProvider(delay=0.3)
    v = await GuardianService(GuardianConfig(timeout_seconds=0.1)).judge(action=_action(), provider=p)
    assert not v.allowed


async def test_guardian_no_provider_denies():
    v = await GuardianService().judge(action=_action(), provider=None)
    assert not v.allowed


def test_circuit_breaker_aborts_on_consecutive_denials():
    cb = RejectionCircuitBreaker()
    deny = GuardianVerdict("high", "low", "deny")
    allow = GuardianVerdict("low", "high", "allow")
    cb.record(deny)
    cb.record(deny)
    assert cb.should_abort() is False
    cb.record(deny)  # 3rd consecutive
    assert cb.should_abort() is True
    # An allow resets the consecutive counter.
    cb.record(allow)
    assert cb.should_abort() is False


def test_circuit_breaker_aborts_on_window_limit():
    cb = RejectionCircuitBreaker(consecutive_limit=99, window_limit=3, window_size=10)
    deny = GuardianVerdict("high", "low", "deny")
    allow = GuardianVerdict("low", "high", "allow")
    for _ in range(3):
        cb.record(deny)
    cb.record(allow)
    cb.record(allow)
    for _ in range(3):
        cb.record(deny)  # 3 denials in window, but consecutive reset by allows
    assert cb.should_abort() is True  # window_limit hit (>=3 in last 10)
