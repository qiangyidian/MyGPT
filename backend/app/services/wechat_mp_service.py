"""WeChat Official Account scan-to-login: issue, redeem, bind.

Design notes that are load-bearing (see app/core/wechat_mp.py for the code
derivation itself):

* **Fail closed on collision.** Derived codes can collide (two openids, same
  window, same 6 digits). Writing the mapping with SET NX and giving up when
  the slot is taken by a *different* openid is what prevents the earlier
  user's code from resolving to the later user's account.
* **Rate-limit before consuming.** The limiter runs first so a throttled guess
  cannot destroy the code a legitimate user is holding — and so the one guess
  that lands cannot sail through the gate.
* **Atomic redemption.** A code is single-use; `GETDEL` is used when the server
  supports it (Redis >= 6.2) with a get-then-delete fallback so an older
  `redis-server` degrades to a non-atomic read rather than failing the login
  endpoint outright.
"""
from __future__ import annotations

import logging
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.redis import get_redis
from app.core.security import hash_password
from app.core.wechat_mp import derive_login_code
from app.models import User, WechatIdentity

logger = logging.getLogger(__name__)

CODE_KEY_PREFIX = "mychat:wechat_mp:code"
FAIL_KEY_PREFIX = "mychat:wechat_mp:fail"

# Suffix appended to a synthesized email so the account is recognisable in the
# admin user list. `.local` is a special-use domain that EmailStr rejects, which
# is why UserOut.email is plain `str` — see schemas/auth.py.
_SYNTHETIC_EMAIL_DOMAIN = "wechat.local"


class WechatLoginError(Exception):
    """User-safe failure (bad code, throttled, binding conflict)."""


async def issue_login_code(openid: str, create_time: int) -> str | None:
    """Derive and register the login code for ``openid``.

    Returns the code, or None when it must not be handed out (collision with
    another openid's live code, or WeChat login is unconfigured).
    """
    settings = get_settings()
    if not settings.WECHAT_MP_TOKEN:
        return None

    code = derive_login_code(openid, create_time, settings.WECHAT_MP_TOKEN)
    key = f"{CODE_KEY_PREFIX}:{code}"
    redis = get_redis()
    # NX: never replace a mapping that belongs to somebody else.
    stored = await redis.set(key, openid, nx=True, ex=settings.WECHAT_MP_CODE_TTL_SECONDS)
    if stored:
        return code
    # Already present. Same owner means WeChat re-pushed the same message (or
    # the follower asked twice in one window) — idempotent, keep the code.
    if await redis.get(key) == openid:
        return code
    logger.warning(
        "wechat login code collision in this window; withholding the code "
        "(no mapping was replaced)"
    )
    return None


async def _consume_code(code: str) -> str | None:
    """Atomically read-and-delete ``code``, returning the bound openid."""
    if not code:
        return None
    key = f"{CODE_KEY_PREFIX}:{code.strip()}"
    redis = get_redis()
    try:
        return await redis.getdel(key)
    except Exception:
        # Redis < 6.2 has no GETDEL. Fall back to a non-atomic read so an older
        # server degrades instead of breaking the login endpoint.
        value = await redis.get(key)
        if value is not None:
            await redis.delete(key)
        return value


async def _enforce_login_limit(ip_address: str | None) -> None:
    if not ip_address:
        return
    settings = get_settings()
    redis = get_redis()
    attempts = int(await redis.get(f"{FAIL_KEY_PREFIX}:{ip_address}") or 0)
    if attempts >= settings.WECHAT_MP_LOGIN_MAX_ATTEMPTS:
        raise WechatLoginError("验证码错误次数过多，请稍后再试")


async def _record_login_failure(ip_address: str | None) -> None:
    if not ip_address:
        return
    settings = get_settings()
    redis = get_redis()
    key = f"{FAIL_KEY_PREFIX}:{ip_address}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, settings.WECHAT_MP_LOGIN_LOCKOUT_SECONDS)


async def _user_for_openid(db: AsyncSession, openid: str) -> User | None:
    identity = (
        await db.execute(select(WechatIdentity).where(WechatIdentity.openid == openid))
    ).scalar_one_or_none()
    if identity is None:
        return None
    return await db.get(User, identity.user_id)


async def _unique_username(db: AsyncSession, base: str) -> str:
    """``base``, or ``base`` plus a short random suffix when already taken.

    A bare openid suffix collides often enough across a growing follower list
    that a unique username constraint would otherwise turn a first scan into a
    500.
    """
    candidate = base
    for _ in range(5):
        taken = (
            await db.execute(select(User.id).where(User.username == candidate))
        ).first()
        if taken is None:
            return candidate
        candidate = f"{base}{secrets.token_hex(2)}"
    return f"{base}{secrets.token_hex(6)}"


async def _create_user_for_openid(db: AsyncSession, openid: str) -> User:
    """Auto-register a MyChat account for a never-seen WeChat follower.

    ``users`` requires non-null email / username / password_hash, so all three
    are synthesized. The password is a random value nobody knows — the account
    is reachable only through WeChat until the user sets a real one.
    """
    user = User(
        email=f"wx_{openid}@{_SYNTHETIC_EMAIL_DOMAIN}",
        username=await _unique_username(db, f"微信用户{openid[-6:]}"),
        password_hash=hash_password(secrets.token_urlsafe(32)),
        role="user",
        is_active=True,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        # Concurrent first scan for the same follower (WeChat retries, or the
        # user double-taps): the loser re-reads the winner's row.
        await db.rollback()
        existing = await _user_for_openid(db, openid)
        if existing is not None:
            return existing
        raise
    db.add(WechatIdentity(openid=openid, user_id=user.id))
    await db.commit()
    await db.refresh(user)
    return user


async def login_with_code(
    db: AsyncSession,
    code: str,
    *,
    ip_address: str | None = None,
) -> User:
    """Redeem a scan code for the account it belongs to, registering if new.

    Raises:
        WechatLoginError: bad/expired/replayed code, throttled IP, or a
            disabled account.
    """
    # BEFORE consuming: a throttled guess must not burn a real user's code.
    await _enforce_login_limit(ip_address)
    openid = await _consume_code(code)
    if not openid:
        await _record_login_failure(ip_address)
        raise WechatLoginError("公众号验证码错误或已过期")

    user = await _user_for_openid(db, openid)
    if user is None:
        user = await _create_user_for_openid(db, openid)
    if not user.is_active:
        raise WechatLoginError("账号已被禁用")
    logger.info("wechat scan login for user %s", user.id)
    return user


async def get_binding(db: AsyncSession, user: User) -> str | None:
    """The openid bound to ``user``, or None."""
    identity = (
        await db.execute(select(WechatIdentity).where(WechatIdentity.user_id == user.id))
    ).scalars().first()
    return identity.openid if identity else None


async def bind_openid(db: AsyncSession, user: User, code: str) -> str:
    """Attach the openid behind ``code`` to ``user``.

    Without this, an existing account (an admin, anyone who registered by
    email) that scans the Official Account would be handed a brand-new empty
    account instead of logging back into its own.

    Raises:
        WechatLoginError: bad code, or the openid already belongs to someone
            else (never silently re-pointed).
    """
    openid = await _consume_code(code)
    if not openid:
        raise WechatLoginError("公众号验证码错误或已过期")

    existing = (
        await db.execute(select(WechatIdentity).where(WechatIdentity.openid == openid))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.user_id == user.id:
            return openid  # already bound to this account — idempotent
        raise WechatLoginError("该微信已绑定到其他账号，请先在原账号解绑")

    # One WeChat identity per account: drop this user's previous binding first,
    # otherwise the unique openid index would eventually reject the insert.
    mine = (
        await db.execute(select(WechatIdentity).where(WechatIdentity.user_id == user.id))
    ).scalars().first()
    if mine is not None:
        await db.delete(mine)

    db.add(WechatIdentity(openid=openid, user_id=user.id))
    await db.commit()
    return openid


async def unbind(db: AsyncSession, user: User) -> bool:
    """Remove ``user``'s WeChat binding. True when one existed."""
    identity = (
        await db.execute(select(WechatIdentity).where(WechatIdentity.user_id == user.id))
    ).scalars().first()
    if identity is None:
        return False
    await db.delete(identity)
    await db.commit()
    return True
