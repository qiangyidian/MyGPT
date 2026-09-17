from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMModel


class RegisterRequest(BaseModel):
    email: EmailStr
    username: str = Field(min_length=2, max_length=128)
    password: str = Field(min_length=6, max_length=128)
    # One-time email verification code (required when MAIL_ENABLED is on).
    verification_code: str | None = Field(default=None, min_length=6, max_length=6)


class EmailCodeRequest(BaseModel):
    email: EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


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
