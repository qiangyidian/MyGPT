"""Lightweight per-identity rate limiting as FastAPI dependencies.

Redis-backed when Redis is reachable (correct across multiple workers / processes)
with an in-memory sliding-window fallback for single-process or Redis-less
deployments. No third-party dependency (slowapi / fastapi-limiter deliberately
avoided to keep the dependency tree lean and avoid a network install).

Two factories:
  * :func:`rate_limit_ip`   — keys by client IP (for unauthenticated endpoints
    like login / register, where IP is the only available identity).
  * :func:`rate_limit_user` — keys by authenticated user id (for cost-incurring
    endpoints like chat / retrieval / upload).

Rate limiting is automatically disabled when ``ENV == "test"`` so the test suite
(which fires many requests per test) is unaffected; dev and prod are protected.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Callable

from fastapi import Depends, HTTPException, Request, status

from app.core.config import get_settings
from app.core.deps import get_current_user
from app.models.user import User

# In-memory fallback buckets: identity -> timestamps still inside the window.
_memory: dict[str, list[float]] = defaultdict(list)
_memory_lock = asyncio.Lock()


def _ip_of(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _check(scope: str, identity: str, limit: int, window: int) -> None:
    """Raise HTTP 429 if ``identity`` has exceeded ``limit`` calls in ``window`` s."""
    # Disabled in test env so the suite (many requests/test) never trips a limit.
    if get_settings().ENV == "test":
        return

    key_id = f"{scope}:{identity}"
    redis_rkey = f"rl:{key_id}"

    # Redis path — correct across workers. Any failure falls through to memory.
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        count = await redis.incr(redis_rkey)
        if count == 1:
            await redis.expire(redis_rkey, window)
        if count > limit:
            ttl = await redis.ttl(redis_rkey)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"请求过于频繁，请稍后再试 ({limit}/{window}s)",
                headers={"Retry-After": str(max(ttl, 1))},
            )
        return
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — Redis unavailable -> in-memory fallback
        pass

    # In-memory sliding window (single-process only).
    async with _memory_lock:
        now = time.monotonic()
        bucket = [t for t in _memory[key_id] if t > now - window]
        if len(bucket) >= limit:
            retry = int(bucket[0] + window - now) + 1
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"请求过于频繁，请稍后再试 ({limit}/{window}s)",
                headers={"Retry-After": str(max(retry, 1))},
            )
        bucket.append(now)
        _memory[key_id] = bucket


def rate_limit_ip(limit: int, window: int, scope: str) -> Callable:
    """Per-IP limiter (use for unauthenticated endpoints)."""

    async def _dep(request: Request) -> None:
        await _check(scope, f"ip:{_ip_of(request)}", limit, window)

    return _dep


def rate_limit_user(limit: int, window: int, scope: str) -> Callable:
    """Per-authenticated-user limiter (resolves the user via get_current_user)."""

    async def _dep(request: Request, user: User = Depends(get_current_user)) -> None:
        await _check(scope, f"user:{user.id}", limit, window)

    return _dep
