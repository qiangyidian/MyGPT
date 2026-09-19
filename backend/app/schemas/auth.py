from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMModel


class RegisterRequest(BaseModel):
    email: EmailStr
    username: str = Field(min_length=2, max_length=128)
    # 8 = PASSWORD_MIN_LENGTH and the frontend rule (login/page.tsx). The schema
    # used to say 6, so the length gate lived only in validate_password_strength
    # — one forgot-the-call away from issuing 6-character passwords.
    password: str = Field(min_length=8, max_length=128)
    # One-time email verification code (required when MAIL_ENABLED is on).
    verification_code: str | None = Field(default=None, min_length=6, max_length=6)


class EmailCodeRequest(BaseModel):
    email: EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    # NOT min_length: login validates an existing hash, and a length rule here
    # would lock out accounts created before the policy existed (and make the
    # error read as "bad credentials" for a typo-length password).
    password: str


class ChangePasswordRequest(BaseModel):
    """POST /api/auth/password — 改密（需登录）。

    ``old_password`` 只在账号真的设过密码时必填；公众号扫码自动注册的账号存的是
    「未设置密码」哨兵，没有旧密码可校验（见 auth_service.password_is_set）。
    """

    old_password: str | None = Field(default=None, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class ForgotPasswordRequest(BaseModel):
    """POST /api/auth/password/forgot — 发送重置验证码。"""

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """POST /api/auth/password/reset — 凭验证码设置新密码。"""

    email: EmailStr
    code: str = Field(min_length=6, max_length=6)
    new_password: str = Field(min_length=8, max_length=128)


class AuthMessageOut(BaseModel):
    """找回密码这类端点的统一返回体。

    文案必须与「邮箱是否存在」无关 —— 响应形状一致才有防枚举的效果。
    """

    ok: bool = True
    message: str


class WechatCodeLoginRequest(BaseModel):
    """The 6-digit code the Official Account sent to the follower."""

    # Bounded like every other pre-auth body field: this value goes into a Redis
    # key, so it must not be attacker-unbounded.
    wechat_code: str = Field(min_length=1, max_length=32)


class WechatBindingOut(BaseModel):
    bound: bool
    openid: str | None = None


class DeleteAccountRequest(BaseModel):
    """Account self-deletion (账号注销) requires password re-authentication."""
    password: str


class UserOut(ORMModel):
    id: uuid.UUID
    # Plain `str`, NOT `EmailStr`: accounts created through WeChat (and accounts
    # anonymized by self-deletion) carry synthetically generated addresses on
    # special-use domains — `wx_xxx@wechat.local`, `deleted-xxx@deleted.invalid`
    # — which email-validator REJECTS. Validation on the way out bought nothing
    # and 500'd the admin user list as soon as one such row existed. Inbound
    # addresses are still `EmailStr` on RegisterRequest/LoginRequest.
    email: str
    username: str
    role: str
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
    user: UserOut


class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
