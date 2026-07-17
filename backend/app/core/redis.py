"""Shared async Redis client.

Resolved once from ``settings.REDIS_URL`` and cached for the process lifetime.
Callers should ``await get_redis()`` rather than constructing clients directly
so connection pooling stays centralized.
"""
from __future__ import annotations

from functools import lru_cache

import redis.asyncio as redis

from app.core.config import get_settings


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    """Return the process-wide async Redis client.

    Uses ``decode_responses=True`` so values come back as ``str`` (the common
    case for caching tokens, rate-limit counters, etc.). Binary use cases should
    construct their own client.
    """
    settings = get_settings()
    return redis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
    )


async def close_redis() -> None:
    """Close + drop the cached client (used on app shutdown)."""
    client = get_redis()
    try:
        await client.aclose()
    finally:
        get_redis.cache_clear()
