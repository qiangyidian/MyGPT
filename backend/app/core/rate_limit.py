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
import ipaddress
import time
from collections import defaultdict
from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status

from app.core.config import get_settings
from app.core.deps import get_current_user
from app.models.user import User

# In-memory fallback buckets: identity -> timestamps still inside the window.
_memory: dict[str, list[float]] = defaultdict(list)
_memory_lock = asyncio.Lock()

# Single-round-trip INCR+EXPIRE (an INCR whose EXPIRE is lost would leave a
# key with no TTL — a permanently throttled identity).
_RATELimit_LUA = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return c
"""


def _trusted_proxy_peers() -> list[ipaddress._BaseNetwork]:
    """Networks whose X-Forwarded-For headers we believe.

    Defaults cover the two real topologies: nginx on the same host (loopback)
    and nginx in front of the compose network (RFC1918 + docker ranges). Ops
    can override with TRUSTED_PROXIES (comma-separated CIDRs).
    """
    settings = get_settings()
    raw = (getattr(settings, "TRUSTED_PROXIES", "") or "").strip()
    if raw:
        networks = []
        for part in raw.split(","):
            part = part.strip()
            if part:
                try:
                    networks.append(ipaddress.ip_network(part, strict=False))
                except ValueError:
                    continue
        return networks
    return [
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    ]


def _ip_in_trusted(ip_str: str, networks: list[ipaddress._BaseNetwork]) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def _ip_of(request: Request) -> str:
    """Client IP for rate limiting — XFF honored ONLY from trusted proxies.

    A client-supplied X-Forwarded-For is trivially spoofable; trusting it
    unconditionally let attackers rotate the header to bypass every per-IP
    limit (login brute force, registration floods, email-bombing). We only
    consult XFF when the direct connection peer is itself a trusted proxy,
    and then walk the chain right-to-left, skipping trusted hops, to the
    first untrusted address.
    """
    peer = request.client.host if request.client else "unknown"
    trusted = _trusted_proxy_peers()
    if not _ip_in_trusted(peer, trusted):
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    if not forwarded:
        return peer
    chain = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    for hop in reversed(chain):
        if not _ip_in_trusted(hop, trusted):
            return hop
    # Every hop is a trusted proxy — fall back to the furthest-reported one.
    return chain[0] if chain else peer


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
        try:
            count = int(await redis.eval(_RATELimit_LUA, 1, redis_rkey, window))
        except Exception:  # noqa: BLE001 — eval unavailable (fakeredis/older) → 2-step
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
