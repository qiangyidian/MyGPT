"""Email verification codes: issue, throttle, verify, consume.

Codes live in Redis with a TTL (default 5 min). Two throttles guard each email
address: a resend interval (min seconds between sends) and an hourly burst
cap. Consumption is compare-and-delete so a code is single-use.

Dev/test convenience: when MAIL_ENABLED is false AND the environment is not
production, the issued code is returned to the caller for display (the SQL2ER
"debug echo" pattern) — production never echoes codes.
"""
from __future__ import annotations

import logging
import secrets

from app.core.config import get_settings
from app.core.redis import get_redis
from app.services.mail_service import send_verification_email

logger = logging.getLogger(__name__)

_CODE_NAMESPACE = "mychat:email_code"
_INTERVAL_NAMESPACE = "mychat:email_code_interval"
_BURST_NAMESPACE = "mychat:email_code_burst"
_ATTEMPT_NAMESPACE = "mychat:email_code_attempts"


class EmailCodeError(Exception):
    """Rate-limit / config problems (user-safe message)."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _normalize(email: str) -> str:
    return (email or "").strip().lower()


def _key(ns: str, email: str) -> str:
    return f"{ns}:{email}"


def _generate_code() -> str:
    # 6 digits, crypto-random, no leading-zero ambiguity (always 6 chars).
    return f"{secrets.randbelow(1_000_000):06d}"


async def request_code(email: str, purpose: str = "register") -> dict:
    """Issue + send a code. Returns a response payload for the API layer.

    Raises:
        EmailCodeError: throttled or mail not configured.
        MailServiceError: SMTP failure.
    """
    settings = get_settings()
    email_n = _normalize(email)
    redis = get_redis()

    # Resend interval: one in-flight code per address; SET NX keeps the
    # original TTL if a code is already pending. An interval of 0 disables
    # the gate (explicit opt-out for tests / tight dev loops).
    interval_key = _key(_INTERVAL_NAMESPACE, email_n)
    if settings.EMAIL_CODE_RESEND_INTERVAL > 0:
        got = await redis.set(
            interval_key, "1", nx=True, ex=settings.EMAIL_CODE_RESEND_INTERVAL
        )
        if not got:
            ttl = await redis.ttl(interval_key)
            raise EmailCodeError(
                f"发送过于频繁，请 {max(ttl, 1)} 秒后再试", retry_after=max(ttl, 1)
            )

    # Hourly burst cap: sliding window of send events.
    burst_key = _key(_BURST_NAMESPACE, email_n)
    count = await redis.zcard(burst_key)
    if count >= settings.EMAIL_CODE_BURST_LIMIT:
        raise EmailCodeError("发送次数过多，请 1 小时后再试", retry_after=3600)
    await redis.zadd(burst_key, {f"{purpose}:{secrets.token_hex(4)}": _now()})
    await redis.expire(burst_key, settings.EMAIL_CODE_BURST_WINDOW)

    code = _generate_code()
    await redis.set(
        _key(_CODE_NAMESPACE, email_n), code, ex=settings.EMAIL_CODE_TTL_SECONDS
    )

    await send_verification_email(email_n, code, purpose)

    payload: dict = {"sent": True}
    # Dev/test only: echo the code when SMTP is off (never in production).
    # MUST key off is_prod — ENV values are dev|test|prod, and comparing
    # against a hand-typed "production" once silently disabled this guard.
    if not settings.MAIL_ENABLED and not settings.is_prod:
        payload["debug_code"] = code
    return payload


async def verify_and_consume(email: str, code: str, purpose: str = "register") -> bool:
    """Constant-time compare + single-use consumption. True when valid.

    Brute-force guard: failed comparisons are counted per address (counter
    lives as long as the code). At ``EMAIL_CODE_MAX_ATTEMPTS`` failures the
    pending code is invalidated and :class:`EmailCodeError` is raised — the
    user must request a fresh code, so a 6-digit code cannot be swept within
    its TTL window.
    """
    import hmac

    settings = get_settings()
    email_n = _normalize(email)
    redis = get_redis()
    key = _key(_CODE_NAMESPACE, email_n)
    stored = await redis.get(key)
    if not stored:
        return False
    ok = hmac.compare_digest(stored, (code or "").strip())
    if ok:
        await redis.delete(key)
        # Clear the resend interval so a fresh code can be requested soon
        # after a successful registration (the address is verified now).
        await redis.delete(_key(_INTERVAL_NAMESPACE, email_n))
        await redis.delete(_key(_ATTEMPT_NAMESPACE, email_n))
        return True
    # Failed attempt: count it; invalidate the code past the attempt cap.
    attempts_key = _key(_ATTEMPT_NAMESPACE, email_n)
    attempts = await redis.incr(attempts_key)
    await redis.expire(attempts_key, settings.EMAIL_CODE_TTL_SECONDS)
    if attempts >= settings.EMAIL_CODE_MAX_ATTEMPTS:
        await redis.delete(key)
        await redis.delete(attempts_key)
        raise EmailCodeError("验证码错误次数过多，已失效，请重新获取")
    return False


async def has_pending_code(email: str) -> bool:
    redis = get_redis()
    return bool(await redis.exists(_key(_CODE_NAMESPACE, _normalize(email))))


def _now() -> float:
    import time

    return time.time()
