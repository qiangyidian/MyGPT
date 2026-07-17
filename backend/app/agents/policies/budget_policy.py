"""Hard-stop budgets for agent runs.

Every runtime checks a :class:`BudgetGuard` at each iteration so a runaway
model (looping on tools, a slow round, a huge tool output) is forcibly capped
rather than running unbounded. When a budget is crossed the guard raises
:class:`~app.agents.schemas.BudgetExceeded`; the runtime translates that into a
graceful ``done`` with ``finish_reason="budget"`` instead of looping forever.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.agents.schemas import BudgetExceeded


@dataclass(frozen=True)
class BudgetLimits:
    """The hard caps. Tuned to the plan's defaults; override per-run if needed."""

    max_agent_steps: int = 8       # model<->tool round trips
    max_tool_calls: int = 12       # individual tool invocations
    max_replan_count: int = 2      # how many times the plan may be revised
    max_runtime_seconds: int = 120
    max_tool_output_chars: int = 8000
    max_total_tokens: int = 40000


DEFAULT_LIMITS = BudgetLimits()


class BudgetGuard:
    """Tracks usage against :class:`BudgetLimits` and raises when crossed."""

    def __init__(self, limits: BudgetLimits | None = None) -> None:
        self.limits = limits or DEFAULT_LIMITS
        self._steps = 0
        self._tool_calls = 0
        self._replans = 0
        self._tokens = 0
        self._start = time.monotonic()

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

    def add_tokens(self, n: int) -> None:
        if n > 0:
            self._tokens += n

    # ---- gates ------------------------------------------------------------
    def check(self) -> None:
        """Raise :class:`BudgetExceeded` if any limit is already crossed."""
        # Steps/tool_calls use strict ``>`` so a limit of N permits exactly N
        # calls (enter_* increments first, then checks). Time/tokens use ``>=``
        # because they're continuous, not counted calls.
        if self._steps > self.limits.max_agent_steps:
            raise BudgetExceeded(
                f"max agent steps ({self.limits.max_agent_steps}) reached"
            )
        if self._tool_calls > self.limits.max_tool_calls:
            raise BudgetExceeded(
                f"max tool calls ({self.limits.max_tool_calls}) reached"
            )
        if self.elapsed_seconds >= self.limits.max_runtime_seconds:
            raise BudgetExceeded(
                f"time budget ({self.limits.max_runtime_seconds}s) exceeded"
            )
        if self._tokens >= self.limits.max_total_tokens:
            raise BudgetExceeded(
                f"token budget ({self.limits.max_total_tokens}) exceeded"
            )

    def enter_step(self) -> None:
        """Call at the top of each model<->tool iteration."""
        self._steps += 1
        self.check()

    def enter_tool_call(self) -> None:
        """Call before each tool invocation."""
        self._tool_calls += 1
        self.check()

    def enter_replan(self) -> None:
        """Call before revising the plan; raises if replans exhausted."""
        if self._replans >= self.limits.max_replan_count:
            raise BudgetExceeded(
                f"max replans ({self.limits.max_replan_count}) reached"
            )
        self._replans += 1

    @property
    def exhausted(self) -> bool:
        try:
            self.check()
        except BudgetExceeded:
            return True
        return False

    @property
    def reason(self) -> str | None:
        try:
            self.check()
        except BudgetExceeded as exc:
            return exc.reason
        return None

    def snapshot(self) -> dict:
        return {
            "steps": self._steps,
            "tool_calls": self._tool_calls,
            "replans": self._replans,
            "tokens_used": self._tokens,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "limits": {
                "max_agent_steps": self.limits.max_agent_steps,
                "max_tool_calls": self.limits.max_tool_calls,
                "max_replan_count": self.limits.max_replan_count,
                "max_runtime_seconds": self.limits.max_runtime_seconds,
                "max_total_tokens": self.limits.max_total_tokens,
            },
        }
