"""LLM verifier：模型验收步骤产出，非法输出一律回退规则版。

核心保证：**verifier 永远返回一个合法 verdict**。模型胡说、超时、报错时，
运行不该因为验收环节而中断。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.agents.workflow.llm_verifier import LLMVerifier, _parse_verdict_json
from app.agents.workflow.planner import build_deep_research_plan
from app.agents.workflow.schemas import (
    Plan,
    StepObservation,
    VerificationVerdict,
)


class _StubProvider:
    def __init__(self, content: str, *, fail: Exception | None = None) -> None:
        self.content = content
        self.fail = fail
        self.calls = 0

    async def chat(self, messages, options=None):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(
            content=self.content,
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            finish_reason="stop",
        )


def _plan() -> Plan:
    return build_deep_research_plan("q")


def _obs(output: str = "充分的分析结论" * 10) -> dict[str, StepObservation]:
    return {
        "researcher": StepObservation(step_id="researcher", output=output),
        "analyst": StepObservation(step_id="analyst", output=output),
        "writer": StepObservation(step_id="writer", output=output),
    }


def test_parse_verdict_accepts_pass():
    r = _parse_verdict_json(
        '{"verdict": "pass", "findings": ["looks good"]}', _plan()
    )
    assert r is not None
    assert r.verdict == VerificationVerdict.pass_


def test_parse_verdict_accepts_revise_with_known_steps():
    r = _parse_verdict_json(
        '{"verdict": "revise", "findings": ["too thin"], '
        '"revise_step_ids": ["analyst"]}',
        _plan(),
    )
    assert r is not None
    assert r.verdict == VerificationVerdict.revise
    assert r.revise_step_ids == ["analyst"]


def test_parse_verdict_rejects_unknown_step_id():
    r = _parse_verdict_json(
        '{"verdict": "revise", "revise_step_ids": ["does-not-exist"]}', _plan()
    )
    assert r is None, "未知 step id 必须被拒（否则 revise 会空转）"


def test_parse_verdict_rejects_revise_without_steps():
    assert _parse_verdict_json('{"verdict": "revise"}', _plan()) is None


def test_parse_verdict_rejects_unknown_verdict():
    assert _parse_verdict_json('{"verdict": "maybe"}', _plan()) is None


def test_parse_verdict_rejects_malformed_json():
    assert _parse_verdict_json("{oops", _plan()) is None


def test_parse_verdict_strips_markdown_fence():
    r = _parse_verdict_json(
        '```json\n{"verdict": "fail", "findings": ["事实错误"]}\n```',
        _plan(),
    )
    assert r is not None
    assert r.verdict == VerificationVerdict.fail


async def test_verifier_uses_model_verdict():
    v = LLMVerifier(
        provider=_StubProvider('{"verdict": "pass", "findings": []}'),
        model_config=None,
    )
    result = await v.verify(_plan(), _obs())
    assert result.verdict == VerificationVerdict.pass_


async def test_verifier_falls_back_on_invalid_output():
    v = LLMVerifier(
        provider=_StubProvider('{"verdict": "nonsense"}'), model_config=None
    )
    result = await v.verify(_plan(), _obs())
    # 规则版对足够长的产出给 pass。
    assert result.verdict == VerificationVerdict.pass_


async def test_verifier_falls_back_on_error():
    v = LLMVerifier(
        provider=_StubProvider("", fail=RuntimeError("boom")), model_config=None
    )
    result = await v.verify(_plan(), _obs())
    assert result.verdict == VerificationVerdict.pass_


async def test_verifier_falls_back_on_timeout(monkeypatch):
    class _Slow:
        async def chat(self, messages, options=None):
            await asyncio.sleep(10)

    from app.core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "AGENT_LLM_VERIFIER_TIMEOUT_S", 0.05, raising=False
    )
    v = LLMVerifier(provider=_Slow(), model_config=None)
    result = await v.verify(_plan(), _obs())
    assert result.verdict == VerificationVerdict.pass_


async def test_verifier_falls_back_to_revise_for_thin_output():
    """规则版仍然生效：产出过短时回退结果是 revise 而非 pass。"""
    v = LLMVerifier(provider=_StubProvider("{bad"), model_config=None)
    result = await v.verify(_plan(), _obs(output=""))
    assert result.verdict == VerificationVerdict.revise


async def test_verifier_skips_model_when_observations_empty():
    """没有观测可验收时不该浪费一次模型调用。"""
    provider = _StubProvider('{"verdict": "pass"}')
    v = LLMVerifier(provider=provider, model_config=None)
    result = await v.verify(_plan(), {})
    assert provider.calls == 0
    assert result.verdict == VerificationVerdict.revise


async def test_verifier_rejects_unknown_step_in_plan():
    """模型给出的 revise_step_ids 含未知 id 时回退，而不是传给 planner。"""
    v = LLMVerifier(
        provider=_StubProvider(
            '{"verdict": "revise", "revise_step_ids": ["ghost"]}'
        ),
        model_config=None,
    )
    result = await v.verify(_plan(), _obs())
    assert result.verdict == VerificationVerdict.pass_, "应回退到规则版"


def test_llm_verifier_flag_defaults_off():
    from app.core.config import get_settings

    s = get_settings()
    assert s.AGENT_LLM_VERIFIER is False
    assert s.AGENT_LLM_VERIFIER_TIMEOUT_S == 10.0
