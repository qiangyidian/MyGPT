"""Security primitives: password hashing, JWT issue/decode, API-key encryption.

Kept free of FastAPI/DB deps so it is unit-testable in isolation.
"""
from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import get_settings

settings = get_settings()

# ---- Password hashing (argon2) --------------------------------------------
_pwd = CryptContext(schemes=["argon2"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd.verify(plain, hashed)
    except Exception:
        return False


# ---- JWT -------------------------------------------------------------------
ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"


def _create_token(subject: str, token_type: str, expires_delta: timedelta, extra: dict | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_access_token(subject: str, extra: dict | None = None) -> str:
    return _create_token(
        subject, ACCESS_TOKEN_TYPE,
        timedelta(minutes=settings.JWT_ACCESS_EXPIRE_MINUTES), extra,
    )


def create_refresh_token(subject: str, extra: dict | None = None) -> str:
    return _create_token(
        subject, REFRESH_TOKEN_TYPE,
        timedelta(days=settings.JWT_REFRESH_EXPIRE_DAYS), extra,
    )


def decode_token(token: str) -> dict[str, Any]:
    """Raises JWTError on invalid/expired tokens."""
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])


# ---- API key encryption (Fernet) ------------------------------------------
def _fernet() -> Fernet:
    key = settings.FERNET_KEY
    if not key:
        # Dev fallback: deterministic per-process key. NOT safe for prod — set FERNET_KEY.
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return ""


def mask_secret(secret: str, visible: int = 4) -> str:
    """Show only the last `visible` chars of an API key for UI display."""
    if not secret:
        return ""
    if len(secret) <= visible:
        return "*" * len(secret)
    return "*" * (len(secret) - visible) + secret[-visible:]


def build_cookie_params() -> dict[str, Any]:
    """Refresh-token cookie params. Secure only outside dev so local http works."""
    return {
        "key": "refresh_token",
        "httponly": True,
        "samesite": "lax",
        "secure": not settings.is_dev,
        "path": "/api/auth",
    }
