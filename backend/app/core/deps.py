"""FastAPI dependencies for auth and authorization."""
from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import ACCESS_TOKEN_TYPE, decode_token
from app.db import get_db
from app.models import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

CRED = status.HTTP_401_UNAUTHORIZED
FORBID = status.HTTP_403_FORBIDDEN


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not token:
        raise HTTPException(CRED, "Not authenticated")
    try:
        payload = decode_token(token)
    except Exception:
        raise HTTPException(CRED, "Invalid or expired token")
    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise HTTPException(CRED, "Wrong token type")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(CRED, "Invalid token payload")
    user = await db.get(User, uuid.UUID(user_id))
    if user is None:
        raise HTTPException(CRED, "User not found")
    # Disabled accounts lose access IMMEDIATELY (previously a deactivation
    # only blocked new logins — already-issued access tokens kept working for
    # their full 30-minute lifetime).
    if not user.is_active:
        raise HTTPException(CRED, "Account is disabled")
    # Global kill-switch: bumping the user's token_version invalidates every
    # access token issued before it.
    if "ver" in payload and int(payload.get("ver") or 0) != int(
        getattr(user, "token_version", 0) or 0
    ):
        raise HTTPException(CRED, "Token revoked")
    # Logout/kill-switch blacklist (per-token jti; best-effort).
    from app.services.auth_service import is_access_revoked

    if await is_access_revoked(payload):
        raise HTTPException(CRED, "Token revoked")
    return user


async def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(FORBID, "Admin privileges required")
    return user
