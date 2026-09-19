"""LLM 规划器：模型提议计划，模板始终是回退。

这个模块**不会**让引擎依赖模型。契约是：

  * 产出必须通过 :func:`~app.agents.workflow.planner.validate_plan`
    （无环、依赖存在、id 唯一）与步数上限；
  * 任何失败 —— 模型报错、超时、预算耗尽、JSON 非法、plan 非法 ——
    一律回退 :func:`~app.agents.workflow.planner.build_plan_for_profile`。

对调用方而言，``build_plan_with_llm`` 永远返回一个可用的 Plan。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from app.agents.workflow.planner import build_plan_for_profile, validate_plan
from app.agents.workflow.schemas import Plan, Step

logger = logging.getLogger(__name__)

# 模型常把 JSON 包在 markdown 代码块里。剥掉围栏后再解析。
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

_SYSTEM_PROMPT = (
    "You are a planning component. Given a user request, produce a small "
    "execution plan as STRICT JSON. Output JSON only — no prose, no markdown "
    "fences. Schema:\n"
    '{"steps": [{"id": "<slug>", "role": "<agent role>", "name": "<display name>", '
    '"task_description": "<one sentence>", "dependencies": ["<other step id>"]}]}\n'
    "Rules: ids are unique snake_case slugs; dependencies reference existing ids; "
    "the graph must be acyclic; keep it small and only include work that is "
    "genuinely needed. Answer in the user's language for name/task_description."
)


def _strip_fence(raw: str) -> str:
    m = _FENCE_RE.match(raw or "")
    return m.group(1) if m else (raw or "")


def _parse_plan_json(raw: str, profile: str, question: str) -> Plan | None:
    """把模型输出解析成 Plan；任何不合规都返回 None（调用方回退模板）。"""
    from app.core.config import get_settings

    max_steps = int(getattr(get_settings(), "AGENT_LLM_PLANNER_MAX_STEPS", 8))
    try:
        payload = json.loads(_strip_fence(raw))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return None
    if len(raw_steps) > max_steps:
        return None

    steps: list[Step] = []
    for item in raw_steps:
        if not isinstance(item, dict):
            return None
        sid = str(item.get("id") or "").strip()
        if not sid:
            return None
        deps = item.get("dependencies") or []
        if not isinstance(deps, list):
            return None
        steps.append(
            Step(
                id=sid,
                role=str(item.get("role") or ""),
                name=str(item.get("name") or sid),
                task_description=str(item.get("task_description") or ""),
                dependencies=[str(d) for d in deps],
                acceptance_criteria={"min_chars": 1},
            )
        )

    plan = Plan(
        version=1,
        goal=(question or "").strip(),
        profile=profile,
        steps=steps,
        max_replans=1,
    )
    try:
        validate_plan(plan)
    except Exception:
        return None
    return plan


def _charge(guard: Any, usage: Any, model_config: Any, kind: str) -> None:
    """把规划器/verifier 的 token 计入 run 预算（与 stage 同源）。

    **同步调用**：BudgetGuard 的临界区用 RLock，中间不得 await。
    """
    if guard is None or not isinstance(usage, dict) or not usage:
        return
    from app.core.pricing import usage_cost

    cost = usage.get("cost_usd")
    if cost is None:
        cost = usage_cost(getattr(model_config, "model_name", None), usage)
    guard.add_usage(usage, cost_usd=cost, usage_id=f"crewai:{kind}")


async def build_plan_with_llm(
    *,
    provider: Any,
    model_config: Any,
    profile: str,
    question: str,
    guard: Any = None,
    max_steps: int = 8,
) -> Plan:
    """用模型提议一个 plan；不可用时回退模板。**永远返回可用的 Plan**。"""
    from app.agents.policies import BudgetExceeded
    from app.core.config import get_settings
    from app.observability import observe_counter, observe_span

    fallback = build_plan_for_profile(profile, question)

    if provider is None:
        observe_counter("agent.llm_planner", 1, outcome="no_provider")
        return fallback

    # 预算耗尽时不发起调用 —— 规划器不该成为压垮预算的最后一根稻草。
    if guard is not None:
        try:
            guard.check()
        except BudgetExceeded:
            observe_counter("agent.llm_planner", 1, outcome="budget_exhausted")
            logger.info("LLM planner skipped: budget exhausted")
            return fallback

    timeout_s = float(getattr(get_settings(), "AGENT_LLM_PLANNER_TIMEOUT_S", 8.0))
    from app.providers.base import ChatOptions

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Profile: {profile}\nRequest: {question}\n"
            f"Produce at most {max_steps} steps.",
        },
    ]
    options = ChatOptions(temperature=0.2, max_tokens=1024)

    try:
        with observe_span("agent.llm_plan", profile=profile):
            async with asyncio.timeout(timeout_s):
                result = await provider.chat(messages, options)
    except TimeoutError:
        observe_counter("agent.llm_planner", 1, outcome="timeout")
        logger.info("LLM planner timed out; using template plan")
        return fallback
    except asyncio.CancelledError:
        raise
    except Exception:
        observe_counter("agent.llm_planner", 1, outcome="error")
        logger.warning("LLM planner call failed; using template plan", exc_info=True)
        return fallback

    _charge(guard, getattr(result, "usage", None), model_config, "planner")

    plan = _parse_plan_json(getattr(result, "content", "") or "", profile, question)
    if plan is None:
        observe_counter("agent.llm_planner", 1, outcome="invalid_plan")
        logger.info("LLM planner produced an invalid plan; using template plan")
        return fallback

    observe_counter("agent.llm_planner", 1, outcome="ok")
    return plan
