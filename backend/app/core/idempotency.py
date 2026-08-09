"""Idempotency for chat sends (and other costly POSTs).

A client-supplied ``Idempotency-Key`` (per user) is deduped within a TTL: the
first send proceeds, a retried send with the same key while it is still in the
window is rejected with 409. This stops the "double-click / flaky-network retry"
failure mode that created duplicate assistant turns and duplicate model spend.

Redis-backed (correct across workers) with an in-memory fallback for the
single-process / Redis-less case. Like rate limiting, disabled in the test env
so the suite is unaffected.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from fastapi import HTTPException, Request, status

from app.core.config import get_settings

# In-memory fallback: user_id|key -> expiry epoch.
_memory: dict[str, float] = defaultdict(float)


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
    key = f"idem:{user_id}:{raw.strip()}"
    ttl = int(get_settings().IDEMPOTENCY_TTL_SECONDS)

    # Redis path.
    try:
        from app.core.redis import get_redis

        client = get_redis()
        # SET NX EX — atomic acquire. Returns True if we got it.
        got = await client.set(key, "1", ex=ttl, nx=True)
        if got:
            return raw
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="请求重复（Idempotency-Key 已在处理中），请勿重复提交",
        )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — Redis unavailable → in-memory fallback
        pass

    # In-memory fallback (single-process only).
    import time as _time

    now = _time.monotonic()
    exp = _memory.get(key, 0.0)
    if exp > now:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="请求重复（Idempotency-Key 已在处理中），请勿重复提交",
        )
    _memory[key] = now + ttl
    return raw
