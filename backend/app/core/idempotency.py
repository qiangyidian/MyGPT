"""Idempotency for chat sends (and other costly POSTs).

A client-supplied ``Idempotency-Key`` (per user) is deduped within a TTL: the
first send proceeds; a retried send with the same key while the first is still
in flight gets a 409. When the first request FAILS, the marker is released so
the client's retry is not blocked for the whole TTL (previously a failed first
attempt locked the key for 10 minutes — the retry could only wait or give up).

Redis-backed (correct across workers) with an in-memory fallback for the
single-process / Redis-less case. Like rate limiting, disabled in the test env
so the suite is unaffected.
"""
from __future__ import annotations

import time
from collections import defaultdict

from fastapi import HTTPException, Request, status

from app.core.config import get_settings

# In-memory fallback: user_id|key -> expiry (monotonic). Bounded — the old
# dict grew without limit for the life of the process.
_memory: dict[str, float] = defaultdict(float)
_MEMORY_MAX_ENTRIES = 10_000

_CONFLICT_DETAIL = "请求重复（Idempotency-Key 已在处理中），请勿重复提交"


def _key(user_id: str | int, raw: str) -> str:
    return f"idem:{user_id}:{raw.strip()}"


def _ttl() -> int:
    return max(int(get_settings().IDEMPOTENCY_TTL_SECONDS), 1)


async def check(request: Request, user_id: str | int) -> str | None:
    """If the request carries an Idempotency-Key, dedupe it.

    Returns the key (and records it) on first use; raises 409 on a duplicate
    within the TTL. No-op when no header is present or in the test env.
    """
    if get_settings().ENV == "test":
        return None
    raw = request.headers.get("Idempotency-Key")
    if not raw:
        return None
    key = _key(user_id, raw)
    ttl = _ttl()

    # Redis path — SET NX EX is the atomic acquire.
    try:
        from app.core.redis import get_redis

        client = get_redis()
        got = await client.set(key, "1", ex=ttl, nx=True)
        if got:
            return raw
        raise HTTPException(status.HTTP_409_CONFLICT, _CONFLICT_DETAIL)
    except HTTPException:
        raise
    except Exception:
        pass

    # In-memory fallback (single-process only).
    now = time.monotonic()
    exp = _memory.get(key, 0.0)
    if exp > now:
        raise HTTPException(status.HTTP_409_CONFLICT, _CONFLICT_DETAIL)
    _memory[key] = now + ttl
    # Bound the fallback map: drop expired entries when it grows too big.
    if len(_memory) > _MEMORY_MAX_ENTRIES:
        for k in [k for k, e in _memory.items() if e <= now][: _MEMORY_MAX_ENTRIES // 2]:
            _memory.pop(k, None)
    return raw


async def release(request: Request, user_id: str | int) -> None:
    """Release a previously-acquired Idempotency-Key (first attempt failed).

    Must be called from the failing path so the client's legitimate retry can
    proceed immediately instead of being 409'd until the TTL lapses.
    """
    raw = request.headers.get("Idempotency-Key")
    if not raw:
        return
    key = _key(user_id, raw)
    try:
        from app.core.redis import get_redis

        client = get_redis()
        await client.delete(key)
        return
    except Exception:
        pass
    _memory.pop(key, None)
