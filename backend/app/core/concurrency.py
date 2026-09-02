"""Backpressure + circuit-breaker guards for outbound model calls.

* :data:`model_limiter` — a process-wide :class:`asyncio.Semaphore` bounding how
  many model calls run concurrently. Without it a burst of chat turns can
  exhaust the DB connection pool and the upstream provider's connection limit
  (unbounded in-flight turns). Sized from ``MAX_CONCURRENT_MODEL_CALLS``.

* :class:`CircuitBreaker` — a per-key (e.g. per provider endpoint) breaker that
  OPENs after ``failure_threshold`` consecutive failures (fast-fails subsequent
  calls instead of queuing behind a dead endpoint), and half-opens after a
  cooldown. A downed provider no longer fails chat hard for everyone for the
  full timeout window.

Both are dependency-free and lazy (the semaphore is created on first use so test
imports stay cheap).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_semaphore: asyncio.Semaphore | None = None


def model_limiter() -> asyncio.Semaphore:
    """Process-wide concurrency limit for in-flight model calls."""
    global _semaphore
    if _semaphore is None:
        from app.core.config import get_settings
        size = max(1, int(getattr(get_settings(), "MAX_CONCURRENT_MODEL_CALLS", 16)))
        _semaphore = asyncio.Semaphore(size)
    return _semaphore


class CircuitBreakerOpenError(RuntimeError):
    """Raised when a call is rejected because the breaker for a key is OPEN."""


class CircuitBreaker:
    """Simple per-key circuit breaker (closed → open → half-open).

    Not a full Hystrix clone, but closes the "downed provider fails chat hard for
    the full timeout window" gap: after N consecutive failures the breaker opens
    and rejects calls immediately for the cooldown, then allows one probe.
    """

    def __init__(self, *, failure_threshold: int = 5, cooldown_seconds: float = 30.0) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        # key -> {"failures": int, "opened_at": float | None}
        self._state: dict[str, dict[str, Any]] = {}

    def _allow(self, key: str, now: float) -> bool:
        st = self._state.get(key)
        if not st or st["opened_at"] is None:
            return True
        # Half-open after cooldown: allow ONE probe.
        if now - st["opened_at"] >= self.cooldown_seconds:
            st["opened_at"] = None
            return True
        return False

    def before(self, key: str) -> None:
        """Call before a guarded operation. Raises if the breaker is open."""
        now = time.monotonic()
        if not self._allow(key, now):
            raise CircuitBreakerOpenError(
                f"circuit breaker open for {key!r} (coolown {self.cooldown_seconds}s)"
            )

    def record_success(self, key: str) -> None:
        self._state.pop(key, None)

    def record_failure(self, key: str) -> None:
        st = self._state.setdefault(key, {"failures": 0, "opened_at": None})
        st["failures"] += 1
        if st["failures"] >= self.failure_threshold:
            st["opened_at"] = time.monotonic()
            logger.warning("circuit breaker OPEN for %r after %d failures", key, st["failures"])


# Module-level breaker used by the provider layer. Threshold/cooldown from settings.
_breaker: CircuitBreaker | None = None


def model_breaker() -> CircuitBreaker:
    global _breaker
    if _breaker is None:
        from app.core.config import get_settings
        s = get_settings()
        _breaker = CircuitBreaker(
            failure_threshold=int(getattr(s, "MODEL_CIRCUIT_FAILURE_THRESHOLD", 5)),
            cooldown_seconds=float(getattr(s, "MODEL_CIRCUIT_COOLDOWN_SECONDS", 30.0)),
        )
    return _breaker
