"""LLM 规划器：模型可以提议，但模板始终是回退。

核心保证：**模型产出非法或调用失败时，运行绝不中断** —— 一律回退到
build_plan_for_profile 的模板 plan。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.agents.workflow.llm_planner import _parse_plan_json, build_plan_with_llm
from app.agents.workflow.planner import build_plan_for_profile


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


_VALID = """
{
  "steps": [
    {"id": "researcher", "role": "researcher", "name": "Researcher",
     "task_description": "gather evidence", "dependencies": []},
    {"id": "analyst", "role": "analyst", "name": "Analyst",
     "task_description": "cross-check", "dependencies": ["researcher"]},
    {"id": "writer", "role": "writer", "name": "Writer",
     "task_description": "write the answer", "dependencies": ["analyst"]}
  ]
}
"""


def test_parse_plan_json_accepts_valid_payload():
    plan = _parse_plan_json(_VALID, "deep_research", "q")
    assert plan is not None
    assert plan.step_ids == ["researcher", "analyst", "writer"]
    assert plan.get("analyst").dependencies == ["researcher"]


def test_parse_plan_json_strips_markdown_fence():
    fenced = f"```json\n{_VALID}\n```"
    plan = _parse_plan_json(fenced, "deep_research", "q")
    assert plan is not None
    assert plan.step_ids == ["researcher", "analyst", "writer"]


def test_parse_plan_json_rejects_cycle():
    bad = """
    {"steps": [
      {"id": "a", "dependencies": ["b"]},
      {"id": "b", "dependencies": ["a"]}
    ]}
    """
    assert _parse_plan_json(bad, "x", "q") is None


def test_parse_plan_json_rejects_missing_dependency():
    bad = """
    {"steps": [{"id": "a", "dependencies": ["nope"]}]}
    """
    assert _parse_plan_json(bad, "x", "q") is None


def test_parse_plan_json_rejects_duplicate_ids():
    bad = """
    {"steps": [{"id": "a", "dependencies": []}, {"id": "a", "dependencies": []}]}
    """
    assert _parse_plan_json(bad, "x", "q") is None


def test_parse_plan_json_rejects_malformed_json():
    assert _parse_plan_json("{not json", "x", "q") is None


def test_parse_plan_json_rejects_empty_steps():
    assert _parse_plan_json('{"steps": []}', "x", "q") is None


def test_parse_plan_json_rejects_too_many_steps():
    steps = ",".join(
        f'{{"id": "s{i}", "dependencies": []}}' for i in range(50)
    )
    assert _parse_plan_json(f'{{"steps": [{steps}]}}', "x", "q") is None


async def test_build_plan_uses_llm_output_when_valid():
    provider = _StubProvider(_VALID)
    plan = await build_plan_with_llm(
        provider=provider, model_config=None, profile="deep_research",
        question="q", guard=None, max_steps=8,
    )
    assert provider.calls == 1
    assert plan.step_ids == ["researcher", "analyst", "writer"]


async def test_build_plan_falls_back_on_invalid_output():
    provider = _StubProvider('{"steps": []}')
    plan = await build_plan_with_llm(
        provider=provider, model_config=None, profile="deep_research",
        question="q", guard=None, max_steps=8,
    )
    template = build_plan_for_profile("deep_research", "q")
    assert plan.step_ids == template.step_ids


async def test_build_plan_falls_back_on_provider_error():
    provider = _StubProvider("", fail=RuntimeError("boom"))
    plan = await build_plan_with_llm(
        provider=provider, model_config=None, profile="deep_research",
        question="q", guard=None, max_steps=8,
    )
    template = build_plan_for_profile("deep_research", "q")
    assert plan.step_ids == template.step_ids


async def test_build_plan_falls_back_on_timeout(monkeypatch):
    class _SlowProvider:
        async def chat(self, messages, options=None):
            await asyncio.sleep(10)

    from app.core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "AGENT_LLM_PLANNER_TIMEOUT_S", 0.05, raising=False
    )
    plan = await build_plan_with_llm(
        provider=_SlowProvider(), model_config=None, profile="deep_research",
        question="q", guard=None, max_steps=8,
    )
    template = build_plan_for_profile("deep_research", "q")
    assert plan.step_ids == template.step_ids


async def test_build_plan_skips_llm_when_budget_exhausted():
    """预算耗尽时直接回退模板，不发起调用。"""
    from app.agents.policies import BudgetExceeded, BudgetGuard, BudgetLimits

    guard = BudgetGuard(BudgetLimits(max_total_tokens=1))
    guard.add_usage({"total_tokens": 999})

    provider = _StubProvider(_VALID)
    with pytest.raises(BudgetExceeded):
        guard.check()  # 确认预算确实已耗尽

    plan = await build_plan_with_llm(
        provider=provider, model_config=None, profile="deep_research",
        question="q", guard=guard, max_steps=8,
    )
    assert provider.calls == 0, "预算耗尽时不得发起模型调用"
    assert plan.step_ids == build_plan_for_profile("deep_research", "q").step_ids


async def test_llm_planner_flags_default_off():
    from app.core.config import get_settings

    s = get_settings()
    assert s.AGENT_LLM_PLANNER is False
    assert s.AGENT_LLM_PLANNER_MAX_STEPS == 8
    assert s.AGENT_LLM_PLANNER_TIMEOUT_S == 8.0
