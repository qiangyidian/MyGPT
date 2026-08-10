"""Hard-stop budgets for agent runs.

Every runtime checks a :class:`BudgetGuard` at each iteration so a runaway
model (looping on tools, a slow round, a huge tool output) is forcibly capped
rather than running unbounded. When a budget is crossed the guard raises
:class:`~app.agents.schemas.BudgetExceeded`; the runtime translates that into a
graceful ``done`` with ``finish_reason="budget"`` instead of looping forever.
"""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, fields, replace
from numbers import Real
from threading import RLock
from typing import Any, Mapping

from app.agents.schemas import BudgetExceeded


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Validated immutable hard caps for one run."""

    max_agent_steps: int = 8       # model<->tool round trips
    max_tool_calls: int = 12       # individual tool invocations
    max_replan_count: int = 2      # how many times the plan may be revised
    max_runtime_seconds: float = 120.0
    max_tool_output_chars: int = 8000
    max_total_tokens: int = 40000
    max_cost_usd: float = 5.0

    def __post_init__(self) -> None:
        positive_ints = (
            "max_agent_steps",
            "max_tool_calls",
            "max_tool_output_chars",
            "max_total_tokens",
        )
        for name in positive_ints:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.max_replan_count, bool)
            or not isinstance(self.max_replan_count, int)
            or self.max_replan_count < 0
        ):
            raise ValueError("max_replan_count must be a non-negative integer")
        for name in ("max_runtime_seconds", "max_cost_usd"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, Real)
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        overrides: Mapping[str, Any] | None = None,
        *,
        allow_increase: bool = False,
    ) -> "BudgetLimits":
        limits = cls(
            max_agent_steps=settings.AGENT_MAX_STEPS,
            max_tool_calls=settings.AGENT_MAX_TOOL_CALLS,
            max_replan_count=settings.AGENT_MAX_REPLAN_COUNT,
            max_runtime_seconds=settings.AGENT_MAX_RUNTIME_SECONDS,
            max_tool_output_chars=settings.AGENT_MAX_TOOL_OUTPUT_CHARS,
            max_total_tokens=settings.AGENT_MAX_TOTAL_TOKENS,
            max_cost_usd=settings.AGENT_MAX_COST_USD,
        )
        return limits.with_overrides(overrides or {}, allow_increase=allow_increase)

    def with_overrides(
        self,
        overrides: Mapping[str, Any],
        *,
        allow_increase: bool = False,
    ) -> "BudgetLimits":
        valid = {item.name for item in fields(self)}
        unknown = set(overrides) - valid
        if unknown:
            raise ValueError(f"unknown budget limit(s): {', '.join(sorted(unknown))}")
        candidate = replace(self, **dict(overrides))
        if not allow_increase:
            increased = [
                name
                for name in overrides
                if getattr(candidate, name) > getattr(self, name)
            ]
            if increased:
                raise ValueError(
                    "per-run budget overrides cannot increase policy limits: "
                    + ", ".join(sorted(increased))
                )
        return candidate


DEFAULT_LIMITS = BudgetLimits()


class BudgetGuard:
    """Tracks usage against :class:`BudgetLimits` and raises when crossed."""

    def __init__(self, limits: BudgetLimits | None = None) -> None:
        self.limits = limits or DEFAULT_LIMITS
        self._steps = 0
        self._tool_calls = 0
        self._replans = 0
        self._tokens = 0
        self._cost_usd = 0.0
        self._start = time.monotonic()
        self._lock = RLock()
        self._cumulative: dict[str, tuple[int, float]] = {}
        self._usage_ids: set[str] = set()

    # ---- usage ------------------------------------------------------------
    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._start

    @property
    def steps(self) -> int:
        return self._steps

    @property
    def tool_calls(self) -> int:
        return self._tool_calls

    @property
    def replans(self) -> int:
        return self._replans

    @property
    def tokens_used(self) -> int:
        return self._tokens

    @property
    def cost_usd_used(self) -> float:
        return self._cost_usd

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.limits.max_runtime_seconds - self.elapsed_seconds)

    def add_tokens(self, n: int) -> None:
        self.add_usage(total_tokens=n)

    def add_usage(
        self,
        usage: Mapping[str, Any] | None = None,
        *,
        total_tokens: int | None = None,
        cost_usd: float | None = None,
        cumulative: bool = False,
        source: str | None = None,
        usage_id: str | None = None,
    ) -> None:
        """Add a delta, an idempotent record, or a cumulative source snapshot.

        Normal calls are deltas. ``usage_id`` makes a logical record exact-once;
        ``cumulative=True`` charges only the increase since that source's prior
        snapshot. Invalid or decreasing metering is rejected instead of silently
        corrupting a run's monotonic accounting.
        """
        raw = dict(usage or {})
        if total_tokens is None:
            total_tokens = raw.get("total_tokens")
            if total_tokens is None and (
                raw.get("prompt_tokens") is not None
                or raw.get("completion_tokens") is not None
            ):
                total_tokens = (raw.get("prompt_tokens") or 0) + (
                    raw.get("completion_tokens") or 0
                )
        if cost_usd is None:
            cost_usd = raw.get("cost_usd")
        tokens = self._validate_tokens(total_tokens)
        cost = self._validate_cost(cost_usd)
        if cumulative and not source:
            raise ValueError("cumulative usage requires a source")

        with self._lock:
            if usage_id is not None and str(usage_id) in self._usage_ids:
                return
            if cumulative:
                key = str(source)
                old_tokens, old_cost = self._cumulative.get(key, (0, 0.0))
                if tokens < old_tokens or cost < old_cost:
                    raise ValueError("cumulative usage must be monotonic")
                self._cumulative[key] = (tokens, cost)
                tokens -= old_tokens
                cost -= old_cost
            self._tokens += tokens
            self._cost_usd += cost
            if usage_id is not None:
                self._usage_ids.add(str(usage_id))

    @staticmethod
    def _validate_tokens(value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("total_tokens must be an integer")
        if value < 0:
            raise ValueError("total_tokens must be non-negative")
        return value

    @staticmethod
    def _validate_cost(value: Any) -> float:
        if value is None:
            return 0.0
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("cost_usd must be a finite number")
        cost = float(value)
        if not math.isfinite(cost):
            raise ValueError("cost_usd must be finite")
        if cost < 0:
            raise ValueError("cost_usd must be non-negative")
        return cost

    # ---- gates ------------------------------------------------------------
    def check(self) -> None:
        """Raise :class:`BudgetExceeded` if any limit is already crossed."""
        # Steps/tool_calls use strict ``>`` so a limit of N permits exactly N
        # calls (enter_* increments first, then checks). Time/tokens use ``>=``
        # because they're continuous, not counted calls.
        reason = self._exhaustion_reason()
        if reason is not None:
            raise BudgetExceeded(reason)

    def _exhaustion_reason(self) -> str | None:
        if self._steps > self.limits.max_agent_steps:
            return f"max agent steps ({self.limits.max_agent_steps}) reached"
        if self._tool_calls > self.limits.max_tool_calls:
            return f"max tool calls ({self.limits.max_tool_calls}) reached"
        if self._replans > self.limits.max_replan_count:
            return f"max replans ({self.limits.max_replan_count}) reached"
        if self.elapsed_seconds >= self.limits.max_runtime_seconds:
            return f"time budget ({self.limits.max_runtime_seconds}s) exceeded"
        if self._tokens >= self.limits.max_total_tokens:
            return f"token budget ({self.limits.max_total_tokens}) exceeded"
        if self._cost_usd >= self.limits.max_cost_usd:
            return f"cost budget (${self.limits.max_cost_usd:g}) exceeded"
        return None

    def enter_step(self) -> None:
        """Call at the top of each model<->tool iteration."""
        with self._lock:
            self._steps += 1
        self.check()

    def enter_tool_call(self) -> None:
        """Call before each tool invocation."""
        with self._lock:
            self._tool_calls += 1
        self.check()

    def enter_replan(self) -> None:
        """Call before revising the plan; raises if replans exhausted."""
        with self._lock:
            self._replans += 1
        self.check()

    @property
    def exhausted(self) -> bool:
        try:
            self.check()
        except BudgetExceeded:
            return True
        return False

    @property
    def reason(self) -> str | None:
        return self._exhaustion_reason()

    def snapshot(self) -> dict:
        elapsed = self.elapsed_seconds
        reason = self._exhaustion_reason()
        used = {
            "steps": self._steps,
            "tool_calls": self._tool_calls,
            "replans": self._replans,
            "total_tokens": self._tokens,
            "cost_usd": round(self._cost_usd, 9),
            "elapsed_seconds": round(elapsed, 3),
        }
        remaining = {
            "steps": max(0, self.limits.max_agent_steps - self._steps),
            "tool_calls": max(0, self.limits.max_tool_calls - self._tool_calls),
            "replans": max(0, self.limits.max_replan_count - self._replans),
            "runtime_seconds": round(
                max(0.0, self.limits.max_runtime_seconds - elapsed), 3
            ),
            "tool_output_chars": self.limits.max_tool_output_chars,
            "total_tokens": max(0, self.limits.max_total_tokens - self._tokens),
            "cost_usd": round(
                max(0.0, self.limits.max_cost_usd - self._cost_usd), 9
            ),
        }
        return {
            # Legacy flat counters retained for existing callers.
            "steps": self._steps,
            "tool_calls": self._tool_calls,
            "replans": self._replans,
            "tokens_used": self._tokens,
            "cost_usd_used": round(self._cost_usd, 9),
            "elapsed_seconds": round(elapsed, 3),
            "limits": asdict(self.limits),
            "used": used,
            "remaining": remaining,
            "exhausted": reason is not None,
            "reason": reason,
        }
