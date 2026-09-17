"""Scan login: turn a code into a MyChat account.

The code itself is issued and redeemed by the ``wechat-auth`` service — this
module only owns the part that is genuinely MyChat's: mapping a WeChat openid
to a user row, and the bind/unbind lifecycle around it.

Gone with the move: the code derivation, the collision fail-closed write and the
per-IP brute-force counter. All three belonged to "two backends must compute the
same value", which ceased to exist once a single service became the only issuer
and redeemer.
"""
from __future__ import annotations

import logging
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models import User, WechatIdentity
from app.services import wechat_auth_client
from app.services.wechat_auth_client import WechatAuthError, WechatAuthUnavailable

logger = logging.getLogger(__name__)

# Suffix for the synthesized email of an auto-registered follower. `.local` is a
# special-use domain that EmailStr rejects — which is why UserOut.email is plain
# `str` (see schemas/auth.py).
_SYNTHETIC_EMAIL_DOMAIN = "wechat.local"

# Re-exported so callers do not have to import the client module too.
WechatLoginError = WechatAuthError
WechatLoginUnavailable = WechatAuthUnavailable


async def _user_for_openid(db: AsyncSession, openid: str) -> User | None:
    identity = (
        await db.execute(select(WechatIdentity).where(WechatIdentity.openid == openid))
    ).scalar_one_or_none()
    if identity is None:
        return None
    return await db.get(User, identity.user_id)


async def _unique_username(db: AsyncSession, base: str) -> str:
    """``base``, or ``base`` plus a short random suffix when already taken."""
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
        # Concurrent first login for the same follower: the loser re-reads the
        # winner's row.
        await db.rollback()
        existing = await _user_for_openid(db, openid)
        if existing is not None:
            return existing
        raise
    db.add(WechatIdentity(openid=openid, user_id=user.id))
    await db.commit()
    await db.refresh(user)
    return user


async def login_with_code(db: AsyncSession, code: str) -> User:
    """Redeem a scan code for the account it belongs to, registering if new.

    Raises:
        WechatLoginError: the code is wrong/expired/already used/not for us.
        WechatLoginUnavailable: wechat-auth is down or our credentials failed.
    """
    openid = await wechat_auth_client.verify_code(code)

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
    """Attach the WeChat behind ``code`` to ``user``.

    Without this, an existing account (an admin, anyone who registered by email)
    that scans would be handed a brand-new empty account instead of logging back
    into its own.
    """
    openid = await wechat_auth_client.verify_code(code)

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
