"""Security primitives: password hashing, JWT issue/decode, API-key encryption.

Kept free of FastAPI/DB deps so it is unit-testable in isolation.
"""
from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from jose import jwt
from passlib.context import CryptContext

from app.core.config import get_settings

logger = logging.getLogger(__name__)

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


def validate_password_strength(password: str) -> None:
    """Enforce the configured password policy. Raises ValueError on violation.

    Replaces the implicit "any non-empty string" policy. Min length + (when
    enabled) a basic complexity rule covering upper/lower/digit to resist
    brute-force / credential-stuffing on the login + register endpoints.
    """
    from app.core.exceptions import AppException

    s = get_settings()
    pwd = password or ""
    if len(pwd) < int(getattr(s, "PASSWORD_MIN_LENGTH", 8)):
        raise AppException(400, "password_too_short", f"密码至少需要 {s.PASSWORD_MIN_LENGTH} 个字符")
    if getattr(s, "PASSWORD_REQUIRE_COMPLEXITY", True):
        if not (any(c.islower() for c in pwd) and any(c.isupper() for c in pwd) and any(c.isdigit() for c in pwd)):
            raise AppException(400, "password_too_weak", "密码需包含大写字母、小写字母和数字")


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
# Module-level cache for the dev-fallback Fernet. Without it, _fernet() used to
# generate a FRESH random key on every call, so ciphertext encrypted in one call
# was immediately undecryptable by the next. Caching makes dev at least
# consistent within a single process. Production must set FERNET_KEY (enforced
# at startup by config._guard_default_secrets) and never hits this fallback.
_FALLBACK_FERNET: Fernet | None = None


def _fernet() -> Fernet:
    global _FALLBACK_FERNET
    key = settings.FERNET_KEY
    if key:
        return Fernet(key.encode() if isinstance(key, str) else key)
    # Dev-only fallback: stable per-process random key. NOT safe for prod —
    # config._guard_default_secrets refuses to boot non-dev without FERNET_KEY.
    if _FALLBACK_FERNET is None:
        logger.warning(
            "FERNET_KEY is empty — generating a random per-process key. Any API "
            "key encrypted now will be UNDECRYPTABLE after a process restart. "
            "Set FERNET_KEY to a stable value: python -c \"from cryptography.fernet "
            "import Fernet; print(Fernet.generate_key().decode())\""
        )
        rand_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        _FALLBACK_FERNET = Fernet(rand_key.encode())
    return _FALLBACK_FERNET


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
        # A non-empty ciphertext that won't decrypt means the Fernet key changed
        # (rotation / restart on the random fallback) or the row was tampered
        # with. Log so it isn't silently misread as "the user stored an empty key".
        logger.warning(
            "decrypt_secret: Fernet InvalidToken — key mismatch or tampered "
            "ciphertext; returning empty string"
        )
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
