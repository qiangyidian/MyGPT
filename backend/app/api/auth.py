"""Auth router: register, login, me, refresh (rotating), logout (blacklist + clear cookie).

Refresh tokens travel in an httponly cookie scoped to /api/auth. Access tokens are
returned in the JSON body and sent by clients as a Bearer header.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_current_user
from app.core.security import (
    ACCESS_TOKEN_TYPE,
    REFRESH_TOKEN_TYPE,
    build_cookie_params,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.db import get_db
from app.services import audit_service
from app.models import User
from app.schemas import (
    LoginRequest,
    RefreshResponse,
    RegisterRequest,
    TokenResponse,
    UserOut,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()

REFRESH_COOKIE_NAME = "refresh_token"

CRED = status.HTTP_401_UNAUTHORIZED
CONF = status.HTTP_409_CONFLICT


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Create a new user account and issue tokens immediately."""
    # Uniqueness checks.
    existing = await db.execute(
        select(User).where((User.email == payload.email) | (User.username == payload.username))
    )
    if existing.scalars().first() is not None:
        raise HTTPException(CONF, "Email or username already registered")

    # First registered user becomes the bootstrap admin.
    is_first = (await db.execute(select(User))).scalars().first() is None

    user = User(
        email=payload.email,
        username=payload.username,
        password_hash=hash_password(payload.password),
        role="admin" if is_first else "user",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    await audit_service.log(actor_id=user.id, action="auth:register", target=f"user:{user.id}")
    return _issue_tokens(user, response)


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Authenticate by email/password and set the refresh cookie."""
    res = await db.execute(select(User).where(User.email == payload.email))
    user = res.scalars().first()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(CRED, "Invalid email or password")
    if not user.is_active:
        raise HTTPException(CRED, "Account disabled")

    await audit_service.log(actor_id=user.id, action="auth:login", target=f"user:{user.id}")
    return _issue_tokens(user, response)


@router.get("/me", response_model=UserOut)
async def me(current: User = Depends(get_current_user)) -> User:
    return current


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> RefreshResponse:
    """Read the refresh cookie, validate it, rotate it (issue a new one), and return
    a fresh access token. Rotation mitigates replay: a stolen refresh token is single-use."""
    token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not token:
        raise HTTPException(CRED, "Missing refresh token")

    try:
        payload = decode_token(token)
    except Exception:
        raise HTTPException(CRED, "Invalid or expired refresh token")

    # Reject tokens that were revoked (logout / already rotated).
    from app.services import auth_service
    if not await auth_service.is_refresh_valid(token):
        raise HTTPException(CRED, "Invalid or expired refresh token")

    if payload.get("type") != REFRESH_TOKEN_TYPE:
        raise HTTPException(CRED, "Wrong token type")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(CRED, "Invalid token payload")

    user = await db.get(User, uuid.UUID(user_id))
    if user is None:
        raise HTTPException(CRED, "User not found")
    if not user.is_active:
        raise HTTPException(CRED, "Account disabled")

    # Rotate: revoke the consumed token (so it can't be replayed), then mint a
    # brand-new one carrying a fresh jti (overwrites the cookie).
    await auth_service.revoke_refresh(token)
    new_refresh = create_refresh_token(str(user.id), extra={"jti": uuid.uuid4().hex})
    _set_refresh_cookie(response, new_refresh)

    access = create_access_token(str(user.id))
    return RefreshResponse(
        access_token=access,
        token_type="bearer",
        expires_in=settings.JWT_ACCESS_EXPIRE_MINUTES * 60,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Best-effort blacklist of the presented refresh token, then clear the cookie.

    We validate defensively so logout is idempotent: an invalid/expired/missing token
    still yields 204 (the client's cookie is cleared either way)."""
    token = request.cookies.get(REFRESH_COOKIE_NAME)
    _clear_refresh_cookie(response)
    if not token:
        return

    try:
        payload = decode_token(token)
    except Exception:
        # Invalid/expired token — nothing to blacklist, but we still clear the cookie.
        return

    # Blacklist the refresh token (by its jti) so it can't be reused after logout.
    from app.services import auth_service
    try:
        await auth_service.revoke_refresh(token)
    except Exception:  # noqa: BLE001 — revocation store unavailable; cookie still cleared
        pass


# ---- helpers ---------------------------------------------------------------
def _issue_tokens(user: User, response: Response) -> TokenResponse:
    access = create_access_token(str(user.id))
    refresh_tok = create_refresh_token(str(user.id), extra={"jti": uuid.uuid4().hex})
    _set_refresh_cookie(response, refresh_tok)
    return TokenResponse(
        access_token=access,
        token_type="bearer",
        expires_in=settings.JWT_ACCESS_EXPIRE_MINUTES * 60,
        user=UserOut.model_validate(user),
    )


def _set_refresh_cookie(response: Response, token: str) -> None:
    params = build_cookie_params()
    response.set_cookie(value=token, max_age=settings.JWT_REFRESH_EXPIRE_DAYS * 86400, **params)


def _clear_refresh_cookie(response: Response) -> None:
    params = build_cookie_params()
    response.delete_cookie(**params)
