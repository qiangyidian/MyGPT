"""FastAPI dependencies for auth and authorization."""
from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
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
    return user


async def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(FORBID, "Admin privileges required")
    return user
