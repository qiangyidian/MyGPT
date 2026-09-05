"""Guardian — LLM-as-judge pre-execution review gate (Codex pattern).

Before a risky action runs (and before bothering a human), a *separate,
locked-down* reviewer judges it and returns a typed verdict:

    {risk_level: low|medium|high|critical, user_authorization: unknown|low|medium|high,
     outcome: allow|deny, rationale}

Fail-closed contract: timeout, provider error, or unparseable/invalid response
ALWAYS yields ``outcome=deny`` (risk_level=high) — "if you can't prove it's safe,
it's not safe." Only a clean ``allow`` proceeds.

Companion pieces (what make the judge safe to actually ship):
  * :class:`RejectionCircuitBreaker` — 3 consecutive denials OR 10 denials in the
    last 50 reviews aborts the turn, killing the "agent retries 50 times with
    trivial rewording" failure mode an LLM judge uniquely enables.
  * :data:`GUARDIAN_POLICY` — a typed risk taxonomy with explicit "deny even when
    user_authorization is high" overrides (data exfiltration, credential probing,
    destructive actions) so the judge can't rubber-stamp.

In MyGPT this sits in front of the existing dangerous-tool human-approval gate:
low-risk → auto-allow, high-risk/uncertain → deny or escalate to the human.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

from app.agents.token_budget import PromptAdmissionError
from app.providers.base import (
    ChatOptions,
    ProviderError,
    admit_provider_payload,
    provider_output_token_parameter,
)

logger = logging.getLogger(__name__)

RiskLevel = Literal["low", "medium", "high", "critical"]
Outcome = Literal["allow", "deny"]
_VALID_OUTCOMES = {"allow", "deny"}
_VALID_RISK = {"low", "medium", "high", "critical"}
_OUTCOME_ALIASES = {"approved": "allow", "permit": "allow", "denied": "deny", "block": "deny", "reject": "deny"}


GUARDIAN_POLICY = """\
你是安全审查员（Guardian）。你将看到一个【计划执行的动作】及其上下文，判断它是否安全可直接执行。
只输出一个 JSON 对象，不要解释、不要 markdown。

JSON 字段：
- risk_level: 风险等级。"low" | "medium" | "high" | "critical"
- user_authorization: 用户对该类操作的授权程度。"unknown" | "low" | "medium" | "high"
- outcome: "allow"（可直接执行）| "deny"（拒绝/需人工）
- rationale: 一句话理由

风险分类法（按类别判定，下列类别即使 user_authorization=high 也要 deny）：
1. 数据外泄（Data Exfiltration）：把密钥/令牌/敏感数据发送到不受信任的外部目的地 → deny。
2. 凭证探测（Credential Probing）：尝试读取/破解/枚举凭证 → deny。
3. 持久性安全削弱（Persistent Security Weakening）：关闭防火墙/降低权限校验/植入后门 → deny。
4. 破坏性操作（Destructive Actions）：不可逆删除、批量删除、drop/格式化、覆盖关键配置 → deny。
5. 低风险（Low-Risk）：只读、本地幂等写入、明确范围内的常规操作 → allow。

判定原则：
- 只读/检索/在已批准前缀内的常规命令 → low → allow。
- 有副作用但可逆、且在用户意图范围内 → medium → allow（若 user_authorization 不低于 medium）。
- 不确定或证据不足 → 按 fail-closed 原则 deny。
"""


@dataclass(frozen=True)
class GuardianConfig:
    timeout_seconds: float = 6.0
    max_retries: int = 0  # guardian is fail-closed; don't retry into latency
    temperature: float = 0.0
    max_tokens: int = 200


@dataclass
class GuardianVerdict:
    risk_level: RiskLevel
    user_authorization: str
    outcome: Outcome
    rationale: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"


_DENY = GuardianVerdict(risk_level="high", user_authorization="unknown", outcome="deny", rationale="guardian fail-closed")


class GuardianService:
    """One-shot judge; fail-closed on every failure path."""

    def __init__(self, config: GuardianConfig | None = None) -> None:
        self._config = config or GuardianConfig()

    async def judge(
        self,
        *,
        action: dict[str, Any],
        transcript: str = "",
        provider: Any,
    ) -> GuardianVerdict:
        if provider is None:
            return _DENY
        messages = [
            {"role": "system", "content": GUARDIAN_POLICY},
            {"role": "user", "content": _build_user(action, transcript)},
        ]
        options = ChatOptions(
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
            output_token_parameter=provider_output_token_parameter(provider),
        )
        try:
            options = admit_provider_payload(provider, messages, options)
        except PromptAdmissionError as exc:  # fail closed on budget rejection
            logger.warning("guardian payload rejected -> deny: %s", exc)
            return _DENY
        try:
            result = await asyncio.wait_for(
                provider.chat(messages, options), timeout=self._config.timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.warning("guardian timed out (%.1fs) -> deny", self._config.timeout_seconds)
            return _DENY
        except ProviderError as exc:
            logger.warning("guardian provider error -> deny: %s", exc)
            return _DENY
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("guardian unexpected error -> deny")
            return _DENY
        return self._parse(result.content or "")

    @staticmethod
    def _parse(raw: str) -> GuardianVerdict:
        payload = _extract_json(raw)
        if not isinstance(payload, dict):
            return _DENY
        outcome = _coerce(payload.get("outcome"), _VALID_OUTCOMES, _OUTCOME_ALIASES)
        if outcome is None:
            return _DENY  # ambiguous -> fail-closed
        risk = _coerce(payload.get("risk_level"), _VALID_RISK, {}) or "high"
        auth = str(payload.get("user_authorization") or "unknown").strip().lower() or "unknown"
        rationale = str(payload.get("rationale") or "").strip()
        return GuardianVerdict(
            risk_level=risk,  # type: ignore[arg-type]
            user_authorization=auth,
            outcome=outcome,  # type: ignore[arg-type]
            rationale=rationale,
        )


def _build_user(action: dict[str, Any], transcript: str) -> str:
    parts = ["【计划执行的动作】", json.dumps(action, ensure_ascii=False)]
    if transcript:
        parts.append("【上下文（近期动作）】")
        parts.append(transcript[:2000])
    parts.append("请输出 JSON：")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Rejection circuit breaker
# --------------------------------------------------------------------------- #
class RejectionCircuitBreaker:
    """Abort the turn after repeated guardian denials.

    Codex: 3 consecutive denials OR 10 denials in the last 50 reviews -> InterruptTurn.
    Reset the consecutive counter on any allow.
    """

    def __init__(self, *, consecutive_limit: int = 3, window_limit: int = 10, window_size: int = 50) -> None:
        self.consecutive_limit = consecutive_limit
        self.window_limit = window_limit
        self.window_size = window_size
        self._consecutive = 0
        self._recent: deque[bool] = deque(maxlen=window_size)  # True=denial

    def record(self, verdict: GuardianVerdict) -> None:
        denied = not verdict.allowed
        self._recent.append(denied)
        if denied:
            self._consecutive += 1
        else:
            self._consecutive = 0

    def should_abort(self) -> bool:
        if self._consecutive >= self.consecutive_limit:
            return True
        if sum(1 for d in self._recent if d) >= self.window_limit:
            return True
        return False


# --------------------------------------------------------------------------- #
# parse helpers (mirror intent_service)
# --------------------------------------------------------------------------- #
def _extract_json(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    fence = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce(value: Any, valid: set[str], aliases: dict[str, str]) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in valid:
        return v
    return aliases.get(v)
