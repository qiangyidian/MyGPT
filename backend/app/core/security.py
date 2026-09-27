"""Security primitives: password hashing, JWT issue/decode, API-key encryption.

Kept free of FastAPI/DB deps so it is unit-testable in isolation.
"""
from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timedelta, UTC
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
import jwt
from passlib.context import CryptContext

from app.core.config import (
    _FERNET_GENERATE_HINT,
    fernet_key_problem,
    get_settings,
    is_placeholder_secret,
)

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


# 「未设置密码」哨兵。公众号扫码自动注册的账号在 users.password_hash 里存这个常
# 量，而不是一个谁都不知道的随机哈希：它不是合法的 passlib 散列，verify_password
# 恒 False（拿它登录必然失败），同时 auth_service.password_is_set() 能稳定识别出
# 「这个账号还没设过密码」——不必给 users 加列、不必新增迁移。
PASSWORD_NOT_SET = "!password-not-set"


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
    now = datetime.now(UTC)
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
    """Raises PyJWT's InvalidTokenError on invalid or expired tokens."""
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])


# ---- API key encryption (Fernet) ------------------------------------------
# Module-level cache for the dev-fallback MultiFernet. Without it, _fernet() used
# to generate a FRESH random key on every call, so ciphertext encrypted in one
# call was immediately undecryptable by the next. Caching makes dev at least
# consistent within a single process. Production must set FERNET_KEY (enforced
# at startup by config._guard_default_secrets) and never hits this fallback.
_FALLBACK_FERNET: MultiFernet | None = None
# Cache of built rotators keyed by the resolved key list, so rotating
# ``FERNET_KEYS`` (which requires a restart anyway) does not leave a stale
# MultiFernet holding the previous key set alive in a long-lived process, and a
# misconfigured entry is reported once instead of on every request.
_FERNET_CACHE: tuple[tuple[str, ...], MultiFernet] | None = None


def _fernet() -> MultiFernet:
    """Return the process-wide rotator: key[0] encrypts, every key decrypts.

    Rotation semantics (finding 42): publish
    ``FERNET_KEYS=<new-key>,<old-key>`` and restart. Rows written before the
    rotation still carry the old key's version byte and decrypt via the second
    entry; every row written after it uses the new key. Once no ciphertext
    references the old key it is dropped from the list, which makes the retired
    key unable to decrypt anything it should not.

    ``config._guard_default_secrets`` already rejects bad keys at boot, but a
    process can still reach here with an unvalidated list (tests patch the
    settings object). Re-check lazily so the failure is one Chinese, actionable
    error at this boundary rather than an English ``ValueError`` from
    ``Fernet()`` inside the first request that touches an encrypted column.
    Placeholder values only warn here: refusing them is the boot guard's job
    (non-dev only), and the repo's own demo key must keep working in dev/test.
    """
    global _FALLBACK_FERNET, _FERNET_CACHE
    keys = settings.fernet_keys
    if keys:
        if _FERNET_CACHE is not None and _FERNET_CACHE[0] == tuple(keys):
            return _FERNET_CACHE[1]
        fatal: list[str] = []
        for i, key in enumerate(keys):
            problem = fernet_key_problem(key)
            if problem is None:
                continue
            if is_placeholder_secret(key):
                logger.warning("FERNET_KEY(S) 第 %s 项%s", i + 1, problem)
            else:
                fatal.append(f"第 {i + 1} 项{problem}")
        if fatal:
            raise RuntimeError(
                "FERNET_KEY(S) 不可用：" + "；".join(fatal)
                + "。请改成合法的 Fernet key，生成命令：" + _FERNET_GENERATE_HINT
                + "；轮换时配 FERNET_KEYS=新key,旧key（逗号分隔、新→旧）。"
            )
        try:
            rotator = MultiFernet([Fernet(k.encode()) for k in keys])
        except Exception as exc:  # pragma: no cover - defensive, checks above pass
            raise RuntimeError(
                f"FERNET_KEY(S) 被 cryptography 拒绝：{exc}。每一项都必须是 44 字符的 "
                f"url-safe base64 Fernet key，生成命令：{_FERNET_GENERATE_HINT}"
            ) from exc
        _FERNET_CACHE = (tuple(keys), rotator)
        return rotator
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
        _FALLBACK_FERNET = MultiFernet([Fernet(rand_key.encode())])
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
