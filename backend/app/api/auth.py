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
from app.core.rate_limit import rate_limit_ip
from app.core.security import (
    REFRESH_TOKEN_TYPE,
    build_cookie_params,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.db import get_db
from app.models import User
from app.schemas import (
    DeleteAccountRequest,
    EmailCodeRequest,
    LoginRequest,
    RefreshResponse,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from app.services import audit_service

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()

REFRESH_COOKIE_NAME = "refresh_token"

CRED = status.HTTP_401_UNAUTHORIZED
CONF = status.HTTP_409_CONFLICT


@router.post("/email-code",
             dependencies=[Depends(rate_limit_ip(10, 60, "email_code"))])
async def request_email_code(payload: EmailCodeRequest) -> dict:
    """Send a one-time verification code to the email (for registration).

    Throttled per address (resend interval + hourly burst) on top of the IP
    rate limit. The code is echoed in the response only when SMTP is disabled
    in a non-production environment (dev convenience).
    """
    from app.services import email_code_service
    from app.services.mail_service import MailServiceError

    try:
        return await email_code_service.request_code(payload.email, purpose="register")
    except email_code_service.EmailCodeError as exc:
        raise HTTPException(429, str(exc)) from exc
    except MailServiceError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(rate_limit_ip(5, 60, "register"))])
async def register(
    payload: RegisterRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Create a new user account and issue tokens immediately."""
    # Password policy (replaces implicit "any non-empty string").
    from app.core.security import validate_password_strength
    validate_password_strength(payload.password)
    # Email verification: consume the one-time code (single use). In test/dev
    # without SMTP the code service is inert (no Redis) — accept the legacy
    # no-code path so local flows and the test suite keep working; production
    # (MAIL_ENABLED + real Redis) always enforces.
    from app.services import email_code_service

    code_enforced = get_settings().MAIL_ENABLED
    if code_enforced:
        try:
            consumed = await email_code_service.verify_and_consume(
                payload.email, payload.verification_code, purpose="register"
            )
        except email_code_service.EmailCodeError as exc:
            # Attempt-cap exhaustion invalidates the code — tell the user to
            # re-request instead of a generic 500.
            raise HTTPException(400, str(exc))
        if not consumed:
            raise HTTPException(400, "验证码错误或已过期")
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


@router.post("/login", response_model=TokenResponse,
             dependencies=[Depends(rate_limit_ip(10, 60, "login"))])
async def login(
    payload: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Authenticate by email/password and set the refresh cookie."""
    res = await db.execute(select(User).where(User.email == payload.email))
    user = res.scalars().first()
    if user is None or not verify_password(payload.password, user.password_hash):
        # Audit failures too: silent failures let credential stuffing run
        # undetected (successes only were logged before).

        await audit_service.log(
            actor_id=user.id if user is not None else None,
            action="auth:login_failed",
            target=f"email:{payload.email.strip().lower()}",
        )
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
    authorization: str | None = None,
) -> None:
    """Best-effort blacklist of the presented refresh token, then clear the cookie.

    We validate defensively so logout is idempotent: an invalid/expired/missing token
    still yields 204 (the client's cookie is cleared either way). When the client
    also sends its ``Authorization: Bearer`` access token, that token's jti is
    blacklisted too — previously a logged-out access token stayed valid for its
    full remaining lifetime.
    """
    if authorization and authorization.lower().startswith("bearer "):
        from app.services.auth_service import blacklist_access_token

        try:
            await blacklist_access_token(authorization[7:].strip())
        except Exception:  # noqa: BLE001 — best-effort revocation
            pass
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

@router.delete("/me", status_code=204)
async def delete_my_account(
    payload: DeleteAccountRequest,
    response: Response,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """账号注销：re-authenticate, purge owned content, anonymize the row.

    Compliance path (GDPR / 个保法): previously there was NO way for a user to
    delete their account or content. The user row is kept (audit-trail
    integrity) but anonymized + disabled, with ``token_version`` bumped so
    every issued token dies immediately.
    """
    from sqlalchemy import delete as _delete
    from sqlalchemy import select as _select

    from app.models import (
        AgentRun,
        Artifact,
        ChatAttachment,
        Conversation,
        KnowledgeBase,
        Project,
        UserMemory,
    )
    from app.services import attachment_service
    from app.services.auth_service import verify_password as _verify

    if not _verify(payload.password, current.password_hash):
        raise HTTPException(CRED, "密码不正确")

    user_id = current.id

    # Gather attachment blobs (files must be deleted by key after rows go).
    att_rows = (
        await db.execute(
            _select(ChatAttachment.id, ChatAttachment.storage_key).where(
                ChatAttachment.user_id == user_id
            )
        )
    ).all()
    attachment_ids = [r[0] for r in att_rows]
    storage_keys = [r[1] for r in att_rows]

    # Knowledge-base vector collections (before the rows are removed).
    kb_ids = (
        (await db.execute(_select(KnowledgeBase.id).where(KnowledgeBase.user_id == user_id)))
        .scalars().all()
    )

    # Delete owned content (explicit order; best-effort per family).
    await db.execute(_delete(Conversation).where(Conversation.user_id == user_id))
    await db.execute(_delete(KnowledgeBase).where(KnowledgeBase.user_id == user_id))
    await db.execute(_delete(Project).where(Project.user_id == user_id))
    await db.execute(_delete(UserMemory).where(UserMemory.user_id == user_id))
    await db.execute(_delete(Artifact).where(Artifact.owner_id == user_id))
    # Stray runs (older runs keep FK history to conversations; conversations
    # cascade handles most, but delete any leftovers scoped to this user).
    await db.execute(_delete(AgentRun).where(AgentRun.user_id == user_id))

    # Anonymize + disable the account (row retained for audit integrity).
    suffix = uuid.uuid4().hex[:12]
    current.email = f"deleted-{suffix}@deleted.invalid"
    current.username = f"deleted-{suffix}"
    current.password_hash = hash_password(uuid.uuid4().hex)
    current.is_active = False
    current.token_version = int(current.token_version or 0) + 1
    await db.commit()

    # Blob + vector cleanup AFTER commit (orphans are swept, never block).
    try:
        await attachment_service.delete_files_for_keys(storage_keys, attachment_ids)
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.rag.qdrant_store import get_vector_store
        from app.rag.rag_service import collection_name

        store = get_vector_store()
        for kb_id in kb_ids:
            try:
                await store.drop_collection(collection_name(kb_id))
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass

    _clear_refresh_cookie(response)
    await audit_service.log(
        actor_id=user_id, action="auth:account_deleted", target=f"user:{user_id}"
    )
