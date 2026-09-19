"""LLM verifier：模型验收步骤产出，非法输出一律回退规则版。

:class:`~app.agents.workflow.verifier.RuleBasedVerifier` 只校验
``min_chars`` —— 它能放过一段 100 字的空话，拦不住偏离问题的长回答。
本类让模型按每步的 ``acceptance_criteria`` 与产出正文做判断。

**契约**：``verify`` 永远返回一个合法 verdict。模型报错、超时、输出非法、
``revise_step_ids`` 含未知 step —— 全部回退 ``RuleBasedVerifier``。
运行绝不因为验收环节而中断。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from app.agents.workflow.schemas import (
    Plan,
    StepObservation,
    VerificationVerdict,
    VerifierResult,
)
from app.agents.workflow.verifier import RuleBasedVerifier

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

_SYSTEM_PROMPT = (
    "You are a verification component. You are given a plan's steps and each "
    "step's produced output. Judge whether the outputs satisfy the work they "
    "were asked to do. Output STRICT JSON only — no prose, no code fences.\n"
    'Schema: {"verdict": "pass"|"revise"|"fail", "findings": ["..."], '
    '"revise_step_ids": ["<step id>"]}\n'
    "Use revise when specific steps need reworking (list them in "
    "revise_step_ids). Use fail only for an unrecoverable problem. Judge the "
    "outputs, not the process. Do not invent step ids that are not in the plan."
)

# 单步产出透给 verifier 的上限：验收不需要读完整篇，但也不能只看摘要。
_OBSERVATION_MAX_CHARS = 4_000


def _strip_fence(raw: str) -> str:
    m = _FENCE_RE.match(raw or "")
    return m.group(1) if m else (raw or "")


def _parse_verdict_json(raw: str, plan: Plan) -> VerifierResult | None:
    """解析模型 verdict；任何不合规返回 None（调用方回退规则版）。"""
    try:
        payload = json.loads(_strip_fence(raw))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    verdict_raw = str(payload.get("verdict") or "").strip().lower()
    verdict = {
        "pass": VerificationVerdict.pass_,
        "revise": VerificationVerdict.revise,
        "fail": VerificationVerdict.fail,
    }.get(verdict_raw)
    if verdict is None:
        return None

    findings_raw = payload.get("findings") or []
    if not isinstance(findings_raw, list):
        return None
    findings = [str(f) for f in findings_raw]

    revise_raw = payload.get("revise_step_ids") or []
    if not isinstance(revise_raw, list):
        return None
    revise_ids = [str(s) for s in revise_raw]

    known = set(plan.step_ids)
    if any(sid not in known for sid in revise_ids):
        # 未知 id 会让 planner 空转甚至抛错 —— 宁可回退也不用。
        return None
    if verdict == VerificationVerdict.revise and not revise_ids:
        # revise 但不指明步骤，planner 无从下手。
        return None

    return VerifierResult(
        verdict=verdict, findings=findings, revise_step_ids=revise_ids
    )


class LLMVerifier:
    """用模型验收；不可用时回退 :class:`RuleBasedVerifier`。"""

    def __init__(
        self,
        *,
        provider: Any,
        model_config: Any = None,
        guard: Any = None,
        fallback: Any = None,
    ) -> None:
        self._provider = provider
        self._model_config = model_config
        self._guard = guard
        self._fallback = fallback or RuleBasedVerifier()

    async def verify(
        self, plan: Plan, observations: dict[str, StepObservation]
    ) -> VerifierResult:
        # 没有可验收的观测 → 直接交给规则版（它会判 revise），不浪费一次调用。
        if not observations:
            return await self._fallback.verify(plan, observations)
        if self._provider is None:
            return await self._fallback.verify(plan, observations)

        from app.agents.policies import BudgetExceeded
        from app.core.config import get_settings
        from app.observability import observe_counter, observe_span

        if self._guard is not None:
            try:
                self._guard.check()
            except BudgetExceeded:
                observe_counter("agent.llm_verifier", 1, outcome="budget_exhausted")
                return await self._fallback.verify(plan, observations)

        timeout_s = float(
            getattr(get_settings(), "AGENT_LLM_VERIFIER_TIMEOUT_S", 10.0)
        )
        from app.providers.base import ChatOptions

        try:
            with observe_span("agent.llm_verify", profile=plan.profile or ""):
                async with asyncio.timeout(timeout_s):
                    result = await self._provider.chat(
                        [
                            {"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": self._render(plan, observations)},
                        ],
                        ChatOptions(temperature=0.0, max_tokens=800),
                    )
        except TimeoutError:
            observe_counter("agent.llm_verifier", 1, outcome="timeout")
            logger.info("LLM verifier timed out; using rule-based verifier")
            return await self._fallback.verify(plan, observations)
        except asyncio.CancelledError:
            raise
        except Exception:
            observe_counter("agent.llm_verifier", 1, outcome="error")
            logger.warning(
                "LLM verifier call failed; using rule-based verifier", exc_info=True
            )
            return await self._fallback.verify(plan, observations)

        from app.agents.workflow.llm_planner import _charge

        _charge(
            self._guard, getattr(result, "usage", None), self._model_config, "verifier"
        )

        parsed = _parse_verdict_json(getattr(result, "content", "") or "", plan)
        if parsed is None:
            observe_counter("agent.llm_verifier", 1, outcome="invalid_verdict")
            logger.info("LLM verifier produced an invalid verdict; using rule-based")
            return await self._fallback.verify(plan, observations)

        observe_counter("agent.llm_verifier", 1, outcome="ok")
        return parsed

    @staticmethod
    def _render(plan: Plan, observations: dict[str, StepObservation]) -> str:
        parts = [f"Goal: {plan.goal}", f"Profile: {plan.profile}", "Steps:"]
        for step in plan.steps:
            obs = observations.get(step.id)
            body = (obs.output if obs else "") or ""
            clipped = body[:_OBSERVATION_MAX_CHARS]
            truncated = " [truncated]" if len(body) > _OBSERVATION_MAX_CHARS else ""
            crit = step.acceptance_criteria or {}
            parts.append(
                f"- id={step.id} name={step.name or step.id}\n"
                f"  asked: {step.task_description}\n"
                f"  criteria: {json.dumps(crit, ensure_ascii=False)}\n"
                f"  output{truncated}: {clipped}"
            )
        return "\n".join(parts)
