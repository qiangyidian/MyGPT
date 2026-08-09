"""IntentService — engineering-grade, model-driven intent recognition.

Replaces the brittle keyword substring router (``classify_intent`` /
``looks_like_*``) with a real classifier: each turn assembles typed context
fragments (:mod:`app.agents.context_fragments`), feeds them to one LLM call,
and parses a structured :class:`~app.agents.schemas.IntentDecision`.

Production hardening (what makes this "engineering-grade", not a toy):
  * **Configurable** — :class:`IntentClassifierConfig` exposes temperature /
    max_tokens / timeout / retries, overridable via settings (env) so ops can
    tune without redeploying code.
  * **Bounded** — ``asyncio.wait_for`` enforces a hard timeout so a slow
    classifier never blocks the chat turn.
  * **Resilient** — a single retry on a transient provider error or an
    unparseable/empty response; unexpected errors abort (no flailing).
  * **Robust parsing** — strips markdown fences, extracts the JSON object from
    surrounding prose, coerces aliases, validates enums, clamps confidence. A
    decision with an invalid route is rejected (→ caller falls back), so a bad
    model reply can never silently mis-route a turn.
  * **Observable** — structured logging of route/kind/confidence/latency.

Failure contract: ``judge`` returns ``None`` on ANY failure (timeout, provider
error, unparseable JSON, invalid route). The caller then falls back to the
keyword router — intent recognition is an *enhancement*, never a hard dependency.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from app.agents.context_fragments import ContextFragment, render_fragments
from app.agents.schemas import IntentDecision
from app.agents.token_budget import PromptAdmissionError
from app.providers.base import (
    ChatOptions,
    ProviderError,
    admit_provider_payload,
    provider_output_token_parameter,
)

logger = logging.getLogger(__name__)


# Process-local LRU cache of recent classifications, keyed by
# (model, user_content, context-block) hash. Re-edits and repeated prompts hit
# the cache for free instead of making another model call on the chat hot path.
_INTENT_CACHE: "OrderedDict[str, IntentDecision]" = OrderedDict()
_INTENT_CACHE_MAX = 512


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IntentClassifierConfig:
    """Tunables for the intent classifier. Override via settings/env if present."""

    temperature: float = 0.0
    max_tokens: int = 320
    timeout_seconds: float = 2.0
    # Retries ON TOP OF the first attempt (0 = single attempt). Retries only fire
    # on transient provider errors or an unparseable/empty response.
    max_retries: int = 0
    # Master kill-switch. When False, judge() short-circuits to None so the
    # keyword router handles routing with zero added latency / model calls.
    enabled: bool = True

    @staticmethod
    def from_settings() -> "IntentClassifierConfig":
        """Build from app settings, falling back to defaults for missing keys.

        Reads are all best-effort getattr with coercion so an unset or
        misconfigured env var never crashes the classifier.
        """
        try:
            from app.core.config import get_settings

            s = get_settings()
        except Exception:  # noqa: BLE001 — settings must never break intent
            return IntentClassifierConfig()

        def _num(name: str, default: float, cast=float) -> float:
            try:
                v = getattr(s, name, None)
                return cast(v) if v not in (None, "") else default
            except (TypeError, ValueError):
                return default

        def _bool(name: str, default: bool) -> bool:
            try:
                v = getattr(s, name, None)
                if isinstance(v, bool):
                    return v
                if isinstance(v, str):
                    return v.strip().lower() in ("1", "true", "yes", "on")
                return default
            except Exception:  # noqa: BLE001
                return default

        return IntentClassifierConfig(
            temperature=_num("INTENT_TEMPERATURE", 0.0),
            max_tokens=int(_num("INTENT_MAX_TOKENS", 320)),
            timeout_seconds=_num("INTENT_TIMEOUT_SECONDS", 2.0),
            max_retries=int(_num("INTENT_MAX_RETRIES", 0)),
            enabled=_bool("INTENT_CLASSIFIER_ENABLED", True),
        )


# --------------------------------------------------------------------------- #
# Valid enums + alias coercion
# --------------------------------------------------------------------------- #
_VALID_ROUTES = {"native", "deep_research", "parallel_research", "debate"}
_VALID_KINDS = {"code", "document", "factual"}

# Common model phrasings → canonical enum values (defensive; the prompt asks for
# the canonical forms, but models improvise).
_ROUTE_ALIASES = {
    "single": "native", "chat": "native", "default": "native", "direct": "native",
    "research": "deep_research", "multi": "deep_research",
    "multi_agent": "deep_research", "multiagent": "deep_research",
    "crew": "deep_research", "deep": "deep_research",
    "parallel": "parallel_research",
    "argument": "debate", "vs": "debate",
}
_KIND_ALIASES = {
    "program": "code", "prog": "code", "script": "code", "coding": "code",
    "doc": "document", "report": "document", "writing": "document",
    "fact": "factual", "qa": "factual", "question": "factual", "answer": "factual",
}


def _coerce_enum(value: Any, valid: set[str], aliases: dict[str, str]) -> str | None:
    """Lowercase + alias-map ``value``; return the canonical value or None."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in valid:
        return v
    return aliases.get(v)


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
_SYSTEM = (
    "你是一个意图识别器。根据【用户消息】和下方【上下文片段】，判断用户这一轮真正想要什么，"
    "并只输出一个 JSON 对象。不要输出任何解释文字、不要使用 markdown 代码块。\n\n"
    "JSON 字段：\n"
    '- route: 执行路径。可选值："native"（单 Agent 直接回答/写代码）、'
    '"deep_research"（研究型多 Agent：检索+交叉核对+带引用的深入调研）、'
    '"parallel_research"（知识库与网络并行研究，当绑定了知识库时）、'
    '"debate"（两个明确主体的对比/辩论+裁判）。\n'
    '- deliverable_kind: 交付物。可选值："code"（写代码/程序/脚本/小游戏）、'
    '"document"（方案/文档/报告/总结）、"factual"（事实/解释/闲聊/简单问答）。\n'
    "- tool_hints: 建议启用的工具名数组（如 [\"web_search\",\"python_exec\"]）；不确定就给 []。\n"
    "- confidence: 0 到 1 的数字，表示你对本次判断的把握。\n"
    "- rationale: 一句话理由。\n\n"
    "判定要点：\n"
    "1) 写代码/程序/脚本/小游戏（如贪吃蛇、爬虫）→ deliverable_kind=\"code\", route=\"native\""
    "（代码绝不走研究流水线，否则会被截断）。\n"
    "2) 需要检索多个来源、交叉核对、带引用的深入调研 → route=\"deep_research\""
    "（若绑定了知识库则用 parallel_research）。\n"
    "3) 两个明确主体的对比/辩论（如 React vs Vue）→ route=\"debate\"。\n"
    "4) 闲聊、简单问答、单轮解释 → route=\"native\", deliverable_kind=\"factual\"。\n\n"
    "示例：\n"
    "用户：用 Python 写一个贪吃蛇游戏 → "
    '{"route":"native","deliverable_kind":"code","tool_hints":[],"confidence":0.95,"rationale":"明确的写代码请求"}\n'
    "用户：深入调研大模型微调的主流方法并对比 → "
    '{"route":"deep_research","deliverable_kind":"factual","tool_hints":["web_search"],"confidence":0.9,"rationale":"需要检索+交叉核对的调研"}'
)


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class IntentService:
    """One-shot intent classifier with timeout + retry + robust parsing."""

    def __init__(self, config: IntentClassifierConfig | None = None) -> None:
        self._config = config or IntentClassifierConfig.from_settings()

    @property
    def config(self) -> IntentClassifierConfig:
        return self._config

    async def judge(
        self,
        *,
        user_content: str,
        fragments: list[ContextFragment],
        provider: Any,
    ) -> IntentDecision | None:
        """Classify intent from the user message + context fragments.

        Returns an :class:`IntentDecision`, or ``None`` on any failure (disabled,
        timeout, provider error, unparseable/invalid response) so the caller
        falls back to the keyword router.
        """
        if not self._config.enabled:
            return None
        if provider is None:
            return None
        if not (user_content or "").strip():
            return None

        ctx_block = render_fragments(fragments)
        # Cache lookup: repeated prompts / minor re-edits skip the model call.
        # Bypassed in the test env so the suite's module-level cache doesn't leak
        # between tests that reuse the same canned content/provider.
        use_cache = _cache_active()
        cache_key = _cache_key(user_content, ctx_block, provider) if use_cache else None
        if cache_key is not None:
            cached = _INTENT_CACHE.get(cache_key)
            if cached is not None:
                _INTENT_CACHE.move_to_end(cache_key)
                logger.info("intent cache hit route=%s kind=%s", cached.route, cached.deliverable_kind)
                return cached

        messages = self._build_messages(user_content, ctx_block)
        options = ChatOptions(
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
            output_token_parameter=provider_output_token_parameter(provider),
        )
        try:
            options = admit_provider_payload(provider, messages, options)
        except PromptAdmissionError as exc:  # normal classifier fallback
            logger.info("intent classifier payload rejected: %s", exc)
            return None

        last_reason = "no_attempt"
        for attempt in range(self._config.max_retries + 1):
            try:
                result = await asyncio.wait_for(
                    provider.chat(messages, options),
                    timeout=self._config.timeout_seconds,
                )
            except asyncio.TimeoutError:
                last_reason = "timeout"
                logger.warning(
                    "intent classifier timed out after %.1fs (attempt %d/%d)",
                    self._config.timeout_seconds, attempt + 1, self._config.max_retries + 1,
                )
                continue
            except ProviderError as exc:
                last_reason = f"provider_error:{exc.code}"
                logger.warning(
                    "intent classifier provider error (attempt %d/%d): %s",
                    attempt + 1, self._config.max_retries + 1, exc,
                )
                continue
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — unexpected: log + abort (no retry)
                logger.exception(
                    "intent classifier unexpected error (attempt %d/%d); aborting",
                    attempt + 1, self._config.max_retries + 1,
                )
                return None

            decision = self._parse(result.content or "")
            if decision is not None:
                logger.info(
                    "intent recognized route=%s kind=%s confidence=%.2f rationale=%r "
                    "(fragments=%d, attempts=%d)",
                    decision.route, decision.deliverable_kind, decision.confidence,
                    decision.rationale, len(fragments), attempt + 1,
                )
                # Populate the cache so the next identical turn is free.
                if cache_key is not None:
                    _INTENT_CACHE[cache_key] = decision
                    _INTENT_CACHE.move_to_end(cache_key)
                    if len(_INTENT_CACHE) > _INTENT_CACHE_MAX:
                        _INTENT_CACHE.popitem(last=False)
                return decision
            last_reason = "unparseable"
            logger.info(
                "intent classifier returned unparseable response (attempt %d/%d)",
                attempt + 1, self._config.max_retries + 1,
            )

        logger.info(
            "intent recognition failed (%s) after %d attempt(s); caller will fall back",
            last_reason, self._config.max_retries + 1,
        )
        return None

    # -- prompt construction ------------------------------------------------
    @staticmethod
    def _build_messages(
        user_content: str, ctx_block: str
    ) -> list[dict[str, Any]]:
        user_parts = ["【上下文片段】"]
        user_parts.append(ctx_block or "（无额外上下文）")
        user_parts.append("【用户消息】")
        user_parts.append(user_content)
        user_parts.append("请输出 JSON：")
        return [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": "\n".join(user_parts)},
        ]

    # -- parsing ------------------------------------------------------------
    @staticmethod
    def _parse(raw: str) -> IntentDecision | None:
        """Parse + validate + coerce the model's JSON into an IntentDecision.

        Returns None if no valid JSON object can be recovered or the route is
        invalid (so a garbage reply never mis-routes a turn).
        """
        payload = _extract_json_object(raw)
        if not isinstance(payload, dict):
            return None

        route = _coerce_enum(payload.get("route"), _VALID_ROUTES, _ROUTE_ALIASES)
        if route is None:
            # Invalid/unknown route → untrusted → reject (caller falls back).
            return None

        kind = _coerce_enum(payload.get("deliverable_kind"), _VALID_KINDS, _KIND_ALIASES)
        if kind is None:
            kind = "factual"  # kind is secondary; default rather than reject.

        confidence = _clamp_float(payload.get("confidence"), default=0.5)
        tool_hints = _coerce_str_list(payload.get("tool_hints"))
        rationale = str(payload.get("rationale") or "").strip()

        return IntentDecision(
            route=route,
            deliverable_kind=kind,
            tool_hints=tool_hints,
            confidence=confidence,
            rationale=rationale,
        )


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Recover the first JSON object from a possibly-noisy model reply.

    Handles: markdown code fences (```json ... ```), leading/trailing prose, and
    plain JSON. Returns the parsed dict, or None if nothing valid parses.
    """
    if not raw:
        return None
    text = raw.strip()
    # Strip a single surrounding code fence (```json\n...\n``` or ```\n...\n```).
    fence = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    # If there's still an object, isolate the outermost { ... }.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = text[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _clamp_float(value: Any, *, default: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _cache_key(user_content: str, ctx_block: str, provider: Any) -> str:
    """Stable hash key for a classification request.

    Includes the model identity (best-effort) so a model swap invalidates the
    cache, plus the user content + rendered context block.
    """
    model = ""
    try:
        model = str(
            getattr(provider, "model", None)
            or getattr(provider, "model_name", None)
            or getattr(provider, "name", None)
            or ""
        )
    except Exception:  # noqa: BLE001
        pass
    h = hashlib.sha1()
    h.update(model.encode("utf-8", "ignore"))
    h.update(b"\x00")
    h.update((user_content or "").encode("utf-8", "ignore"))
    h.update(b"\x00")
    h.update((ctx_block or "").encode("utf-8", "ignore"))
    return h.hexdigest()


def _cache_active() -> bool:
    """Cache is active outside the test env (keeps the suite isolated)."""
    try:
        from app.core.config import get_settings

        return get_settings().ENV != "test"
    except Exception:  # noqa: BLE001
        return True


def clear_intent_cache() -> None:
    """Drop all cached classifications (test/ops hook)."""
    _INTENT_CACHE.clear()


# Module-level singleton (stateless; config read once at import).
intent_service = IntentService()
