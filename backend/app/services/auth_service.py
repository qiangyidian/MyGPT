"""Authentication & token lifecycle service.

Responsibilities:
  * register / authenticate users (password hashing via security helpers)
  * issue access (+ refresh) JWTs
  * rotate / validate refresh tokens, blacklisting revoked JTIs

Refresh-token revocation uses a Redis set keyed ``refresh:blacklist``. Redis is
wired lazily from ``settings.REDIS_URL``. If Redis is unavailable (not
installed, or connection refused) the service degrades to an in-memory set so
that local development and tests never hard-fail — a single-process deployment
still gets correct revocation semantics.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import (
    REFRESH_TOKEN_TYPE,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models import User

logger = logging.getLogger(__name__)
settings = get_settings()

# Refresh-token revocation set name in Redis.
REFRESH_BLACKLIST_KEY = "refresh:blacklist"

# ---------------------------------------------------------------------------
# Redis helper (lazy, optional)
# ---------------------------------------------------------------------------
_redis_client: Any = None
_redis_checked = False
# Fallback in-memory blacklist for single-process / test environments.
_mem_blacklist: set[str] = set()


async def _get_redis() -> Any:
    """Return an async Redis client, or None if Redis is unavailable.

    A missing redis package is cached as "permanently unavailable"; a transient
    connection failure is NOT cached so a brief Redis outage at startup doesn't
    silently disable cross-process refresh-token revocation forever."""
    global _redis_client, _redis_checked
    if _redis_client is not None:
        return _redis_client
    if _redis_checked:
        # Only the "package not installed" case is cached permanently below.
        return None
    try:
        # Imported lazily so the module imports cleanly without redis installed.
        from redis.asyncio import Redis  # type: ignore
    except Exception:  # pragma: no cover - optional dep
        _redis_checked = True  # no package → don't keep retrying the import
        logger.warning("redis package not installed; using in-memory refresh blacklist")
        return None
    try:
        client = Redis.from_url(
            settings.REDIS_URL, decode_responses=True, encoding="utf-8"
        )
        # Ping to confirm reachability.
        await client.ping()
        _redis_client = client
        logger.info("Connected to Redis for refresh-token blacklist")
    except Exception as exc:  # pragma: no cover - environment dependent
        # Transient (Redis briefly down): do NOT cache so the next call retries.
        logger.warning("Redis unavailable (%s); using in-memory refresh blacklist", exc)
        return None
    return _redis_client


async def _blacklist_add(jti: str) -> None:
    """Mark a refresh-token JTI as revoked."""
    if not jti:
        return
    client = await _get_redis()
    if client is not None:
        ttl = settings.JWT_REFRESH_EXPIRE_DAYS * 24 * 3600
        # sadd + expire so the set entry disappears once the token could no longer
        # be valid anyway, keeping the set from growing forever.
        await client.sadd(REFRESH_BLACKLIST_KEY, jti)
        await client.expire(REFRESH_BLACKLIST_KEY, ttl)
    else:
        _mem_blacklist.add(jti)


async def _blacklist_has(jti: str) -> bool:
    if not jti:
        return False
    client = await _get_redis()
    if client is not None:
        return bool(await client.sismember(REFRESH_BLACKLIST_KEY, jti))
    return jti in _mem_blacklist


# ---------------------------------------------------------------------------
# Public service API
# ---------------------------------------------------------------------------
async def register(db: AsyncSession, email: str, username: str, password: str) -> User:
    """Create a new user.

    Raises:
        ValueError: on duplicate email or username.
    """
    email_l = email.strip().lower()
    username_l = username.strip()

    # Pre-check for friendlier errors (the unique constraints are the backstop).
    existing = await db.execute(
        select(User).where(or_(User.email == email_l, User.username == username_l))
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        if row.email == email_l:
            raise ValueError("该邮箱已被注册")
        raise ValueError("该用户名已被占用")

    user = User(
        email=email_l,
        username=username_l,
        password_hash=hash_password(password),
        role="user",
        is_active=True,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # Race between pre-check and insert; surface a clear message.
        raise ValueError("该邮箱或用户名已被占用") from exc
    await db.refresh(user)
    return user


async def authenticate(db: AsyncSession, email: str, password: str) -> Optional[User]:
    """Return the User if credentials match and the account is active, else None."""
    email_l = email.strip().lower()
    result = await db.execute(select(User).where(User.email == email_l))
    user = result.scalar_one_or_none()
    if user is None:
        # Constant-ish failure path.
        verify_password(password, "$argon2id$invalid")
        return None
    if not verify_password(password, user.password_hash):
        return None
    if not user.is_active:
        return None
    return user


def issue_tokens(user: User) -> dict[str, Any]:
    """Issue a fresh access token (and refresh token) for ``user``.

    Returns a dict with ``access_token``, ``expires_in`` (seconds), and
    ``refresh_token``. The refresh JTI is embedded in the token payload so the
    revocation check can identify it.
    """
    # Embed a unique jti so individual refresh tokens can be revoked.
    refresh_jti = uuid.uuid4().hex
    access = create_access_token(subject=str(user.id))
    refresh = create_refresh_token(subject=str(user.id), extra={"jti": refresh_jti})
    expires_in = settings.JWT_ACCESS_EXPIRE_MINUTES * 60
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": expires_in,
    }


async def is_refresh_valid(token: str) -> bool:
    """True if ``token`` is a well-formed, non-expired, non-blacklisted refresh token."""
    try:
        payload = decode_token(token)
    except Exception:
        return False
    if payload.get("type") != REFRESH_TOKEN_TYPE:
        return False
    jti = payload.get("jti")
    if jti and await _blacklist_has(jti):
        return False
    return True


async def decode_refresh(token: str) -> Optional[dict[str, Any]]:
    """Decode and validate a refresh token, returning its payload or None."""
    if not await is_refresh_valid(token):
        return None
    try:
        return decode_token(token)
    except Exception:
        return None


async def revoke_refresh(token: str) -> None:
    """Blacklist a refresh token by its JTI (logout / rotation)."""
    try:
        payload = decode_token(token)
    except Exception:
        # Already invalid/expired — nothing to blacklist.
        return
    jti = payload.get("jti")
    if jti:
        await _blacklist_add(jti)


async def rotate_refresh(db: AsyncSession, token: str) -> Optional[dict[str, Any]]:
    """Validate the old refresh token, revoke it, and mint a new pair.

    Returns the new token bundle, or None if the supplied token is invalid.
    """
    payload = await decode_refresh(token)
    if payload is None:
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    user = await db.get(User, uuid.UUID(user_id))
    if user is None or not user.is_active:
        return None
    # Revoke the consumed token before issuing a new one.
    await revoke_refresh(token)
    return issue_tokens(user)
