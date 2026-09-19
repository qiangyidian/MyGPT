"""Auth router: register, login, me, refresh (rotating), logout (blacklist + clear cookie),
password change / forgot / reset.

Refresh tokens travel in an httponly cookie scoped to /api/auth. Access tokens are
returned in the JSON body and sent by clients as a Bearer header.

Every access token is minted by ``auth_service.issue_tokens``, which is what puts
the ``jti`` and ``ver`` claims in it — without them the blacklist and the
token_version kill-switch in ``get_current_user`` have nothing to match against.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_current_user
from app.core.rate_limit import client_ip, rate_limit_ip, rate_limit_user
from app.core.security import (
    REFRESH_TOKEN_TYPE,
    build_cookie_params,
    decode_token,
    hash_password,
    validate_password_strength,
    verify_password,
)
from app.db import get_db
from app.models import User
from app.schemas import (
    AuthMessageOut,
    ChangePasswordRequest,
    DeleteAccountRequest,
    EmailCodeRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RefreshResponse,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserOut,
    WechatBindingOut,
    WechatCodeLoginRequest,
)
from app.services import audit_service, auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()
logger = logging.getLogger(__name__)

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

    # 积分账户：注册即建行（余额 0，或配置的注册赠送）。不建的话
    # "余额为 0" 和 "账户不存在" 在排查时会变成两件事。
    from app.credits import get_credit_policy
    from app.services import credit_service

    await credit_service.get_or_create_account(db, user.id)
    _credit_policy = get_credit_policy()
    if _credit_policy.signup_bonus > 0:
        await credit_service.grant(
            db,
            user.id,
            amount=_credit_policy.signup_bonus,
            reason="signup_bonus",
            ref_type="user",
            ref_id=str(user.id),
            note="注册赠送",
        )
    await db.commit()

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


# ---- WeChat scan login (公众号验证码登录) ------------------------------------
#
# MyChat does not talk to WeChat: the callback, the reply text and the code
# issuance all live in the standalone wechat-auth service, because WeChat allows
# exactly one callback URL per Official Account while several products share the
# account. Here we only redeem a code for an openid. See docs/wechat-login.md.
@router.post("/login/wechat", response_model=TokenResponse,
             dependencies=[Depends(rate_limit_ip(30, 60, "wechat_login"))])
async def login_with_wechat_code(
    payload: WechatCodeLoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Redeem a scan code; auto-registers a first-time follower."""
    from app.services import wechat_login_service
    from app.services.wechat_auth_client import (
        WechatAuthError,
        WechatAuthThrottled,
        WechatAuthUnavailable,
    )

    # Read at request time, not from the module-level `settings` snapshot:
    # a cleared get_settings() cache leaves that snapshot stale.
    if not get_settings().WECHAT_AUTH_ENABLED:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "公众号登录未启用")

    try:
        user = await wechat_login_service.login_with_code(
            db, payload.wechat_code, client_ip=client_ip(request)
        )
    except WechatAuthUnavailable as exc:
        # Our credentials are wrong or the service is down — an operator
        # problem, never reported to the user as "your code is wrong".
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except WechatAuthThrottled as exc:
        # 429, 不是 401：这位用户只是试得太快，不是凭据错了。
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    except WechatAuthError as exc:
        await audit_service.log(
            actor_id=None,
            action="auth:login_wechat_failed",
            target=f"code:{payload.wechat_code.strip()[:8]}",
        )
        raise HTTPException(CRED, str(exc)) from exc

    await audit_service.log(
        actor_id=user.id, action="auth:login_wechat", target=f"user:{user.id}"
    )
    return _issue_tokens(user, response)


@router.get("/wechat/binding", response_model=WechatBindingOut)
async def get_wechat_binding(
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WechatBindingOut:
    from app.services import wechat_login_service

    openid = await wechat_login_service.get_binding(db, current)
    return WechatBindingOut(bound=openid is not None, openid=openid)


@router.post("/wechat/binding", response_model=WechatBindingOut)
async def bind_wechat(
    payload: WechatCodeLoginRequest,
    request: Request,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WechatBindingOut:
    """Attach the scanning WeChat to the CURRENT account.

    Without this, an existing account that scans the Official Account would be
    handed a brand-new empty account instead of logging back into its own.
    """
    from app.services import wechat_login_service
    from app.services.wechat_auth_client import WechatAuthError, WechatAuthUnavailable

    # Read at request time, not from the module-level `settings` snapshot:
    # a cleared get_settings() cache leaves that snapshot stale.
    if not get_settings().WECHAT_AUTH_ENABLED:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "公众号登录未启用")

    try:
        openid = await wechat_login_service.bind_openid(
            db, current, payload.wechat_code, client_ip=client_ip(request)
        )
    except WechatAuthUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except WechatAuthError as exc:
        # An openid owned by somebody else is a conflict, not a bad request —
        # and it is never silently re-pointed at the caller.
        raise HTTPException(CONF, str(exc)) from exc

    await audit_service.log(
        actor_id=current.id, action="auth:wechat_bound", target=f"user:{current.id}"
    )
    return WechatBindingOut(bound=True, openid=openid)


@router.delete("/wechat/binding", response_model=WechatBindingOut)
async def unbind_wechat(
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WechatBindingOut:
    from app.services import wechat_login_service

    removed = await wechat_login_service.unbind(db, current)
    if removed:
        await audit_service.log(
            actor_id=current.id, action="auth:wechat_unbound", target=f"user:{current.id}"
        )
    return WechatBindingOut(bound=False, openid=None)


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

    # 版本闸门：改密 / 管理员停用会 bump token_version，此后旧 refresh token 也换不
    # 出新 access token（只挡 access token 的话，刷新一次就绕过了）。
    # 没有 ver 的历史 token 继续放行，与 get_current_user 的口径一致。
    if "ver" in payload and int(payload.get("ver") or 0) != int(
        user.token_version or 0
    ):
        raise HTTPException(CRED, "Token revoked")

    # Rotate: revoke the consumed token (so it can't be replayed), then mint a
    # brand-new one carrying a fresh jti (overwrites the cookie).
    await auth_service.revoke_refresh(token)
    tokens = auth_service.issue_tokens(user)
    _set_refresh_cookie(response, tokens["refresh_token"])

    return RefreshResponse(
        access_token=tokens["access_token"],
        token_type=tokens["token_type"],
        expires_in=tokens["expires_in"],
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
) -> None:
    """Best-effort blacklist of the presented refresh token, then clear the cookie.

    We validate defensively so logout is idempotent: an invalid/expired/missing token
    still yields 204 (the client's cookie is cleared either way). When the client
    also sends its ``Authorization: Bearer`` access token, that token's jti is
    blacklisted too — previously a logged-out access token stayed valid for its
    full remaining lifetime.

    The header is read off ``request`` on purpose: as a bare ``authorization: str |
    None = None`` parameter FastAPI binds it to a *query* string, so the bearer
    token the client actually sends never reached this function and the access
    half of logout silently did nothing.
    """
    authorization = request.headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        try:
            await auth_service.blacklist_access_token(authorization[7:].strip())
        except Exception:
            pass
    token = request.cookies.get(REFRESH_COOKIE_NAME)
    _clear_refresh_cookie(response)
    if not token:
        return

    try:
        decode_token(token)
    except Exception:
        # Invalid/expired token — nothing to blacklist, but we still clear the cookie.
        return

    # Blacklist the refresh token (by its jti) so it can't be reused after logout.
    try:
        await auth_service.revoke_refresh(token)
    except Exception:
        pass


# ---- 密码：修改 / 找回 ------------------------------------------------------
#
# 这三个端点共用一条不变量：密码一旦变化就 bump token_version，于是所有已签发的
# access token（ver claim）和后续 /refresh 换来的新 token 全部作废。没有这条
# bump，「改密」只是换了一个登录口令，劫持中的会话照样活着。
@router.post("/password", response_model=TokenResponse,
             dependencies=[Depends(rate_limit_user(10, 60, "password_change"))])
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """修改密码（需登录）。校验旧密码，改后旧 token 全部失效并下发新会话。

    公众号扫码自动注册的账号从来没设过密码（users.password_hash 存的是
    ``PASSWORD_NOT_SET`` 哨兵）——对它们「原密码」是不可校验的，所以允许直接设置。
    这条判断只用现有列（password_hash 是否是可解析的散列），没有新增迁移；哨兵方案
    上线之前创建的微信老行存的是一串随机哈希，与「本人设过密码」在现有字段上无法区
    分，因此仍会被要求填原密码 —— 需要一次性 backfill 才能放行，见交付说明。
    """
    validate_password_strength(payload.new_password)
    if auth_service.password_is_set(current):
        if not payload.old_password or not verify_password(
            payload.old_password, current.password_hash
        ):
            await audit_service.log(
                actor_id=current.id,
                action="auth:password_change_failed",
                target=f"user:{current.id}",
            )
            raise HTTPException(CRED, "原密码不正确")
    # password_is_set() False：这个账号没有可校验的原密码（微信扫码自动注册的），
    # old_password 填了也忽略 —— 要求一个不存在的凭据只会把人永久挡在门外。

    await auth_service.set_password(db, current, payload.new_password)

    # 本次请求的 access token / cookie 里的 refresh token 都已随版本失效：
    # 顺手打进黑名单，避免「同一进程内已缓存的 token 再被用一次」。
    authorization = request.headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        try:
            await auth_service.blacklist_access_token(authorization[7:].strip())
        except Exception:
            pass
    old_refresh = request.cookies.get(REFRESH_COOKIE_NAME)
    if old_refresh:
        try:
            await auth_service.revoke_refresh(old_refresh)
        except Exception:
            pass

    await audit_service.log(
        actor_id=current.id, action="auth:password_changed", target=f"user:{current.id}"
    )
    return _issue_tokens(current, response)


@router.post("/password/forgot", response_model=AuthMessageOut,
             dependencies=[Depends(rate_limit_ip(5, 60, "password_forgot"))])
async def forgot_password(
    payload: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> AuthMessageOut:
    """发送找回密码验证码。无论邮箱是否注册，响应都一样（防枚举）。

    限速走两层现成的：IP 级 rate_limit_ip + 邮箱级（重发间隔 / 小时上限）在
    email_code_service 里，另开一套只会让两边口径不一致。
    """
    from app.services import email_code_service
    from app.services.mail_service import MailServiceError

    generic = AuthMessageOut(
        message="如果该邮箱已注册，我们已发送重置验证码，请在 5 分钟内完成重置"
    )
    email = payload.email.strip().lower()
    user = (
        await db.execute(select(User).where(User.email == email))
    ).scalars().first()
    if user is None or not _mail_deliverable(email):
        # 查不到账号 / 合成邮箱收不到信：仍然返回同一句话，也不发信。
        return generic

    try:
        await email_code_service.request_code(email, purpose="reset")
    except email_code_service.EmailCodeError as exc:
        # 发信节流：429 只暴露「这个地址 60 秒内发过一次」，攻击者本来就知情。
        raise HTTPException(429, str(exc)) from exc
    except MailServiceError as exc:
        # SMTP 故障不能变成「这个邮箱存在」的枚举信号 —— 吞掉异常，日志留痕。
        logger.warning("password-forgot mail failed for %s: %s", email, exc)
    return generic


@router.post("/password/reset", response_model=AuthMessageOut,
             dependencies=[Depends(rate_limit_ip(10, 60, "password_reset"))])
async def reset_password(
    payload: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> AuthMessageOut:
    """凭邮箱验证码重置密码。成功同样 bump token_version（劫持中的会话一起死）。

    验证码校验先于账号查询：验证码无效时返回什么错误与邮箱是否存在无关。
    """
    from app.services import email_code_service

    validate_password_strength(payload.new_password)
    try:
        consumed = await email_code_service.verify_and_consume(
            payload.email, payload.code, purpose="reset"
        )
    except email_code_service.EmailCodeError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not consumed:
        raise HTTPException(400, "验证码错误或已过期")

    email = payload.email.strip().lower()
    user = (
        await db.execute(select(User).where(User.email == email))
    ).scalars().first()
    if user is None:
        # 有有效验证码却查不到账号 = 账号在发码之后被删了，不是别人的信。
        raise HTTPException(400, "验证码已失效，请重新获取")
    if not user.is_active:
        raise HTTPException(CRED, "账号已被禁用")

    await auth_service.set_password(db, user, payload.new_password)
    await audit_service.log(
        actor_id=user.id, action="auth:password_reset", target=f"user:{user.id}"
    )
    return AuthMessageOut(message="密码已重置，请用新密码登录")


# ---- helpers ---------------------------------------------------------------
def _mail_deliverable(email: str) -> bool:
    """合成邮箱（微信自动注册 / 已注销账号）收不到信，别白耗一次发信额度。"""
    return not email.endswith(("@wechat.local", "@deleted.invalid"))


def _issue_tokens(user: User, response: Response) -> TokenResponse:
    """Mint an access+refresh pair through the one issuing path and set the cookie.

    Delegates to ``auth_service.issue_tokens`` so every token carries both the
    ``jti`` (logout revokes it) and the ``ver`` (改密 / 停用 kill-switch) claim — a
    locally-minted token without them was exactly how revocation became a no-op.
    """
    tokens = auth_service.issue_tokens(user)
    _set_refresh_cookie(response, tokens["refresh_token"])
    return TokenResponse(
        access_token=tokens["access_token"],
        token_type=tokens["token_type"],
        expires_in=tokens["expires_in"],
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
    except Exception:
        pass
    try:
        from app.rag.qdrant_store import get_vector_store
        from app.rag.rag_service import collection_name

        store = get_vector_store()
        for kb_id in kb_ids:
            try:
                await store.drop_collection(collection_name(kb_id))
            except Exception:
                pass
    except Exception:
        pass

    _clear_refresh_cookie(response)
    await audit_service.log(
        actor_id=user_id, action="auth:account_deleted", target=f"user:{user_id}"
    )
