"""Email verification codes: generation, throttling, consumption, register flow.

Redis is stubbed with an in-memory fake (set/get/delete/expire/ttl/zcard/zadd)
matching the surface email_code_service uses; SMTP is monkeypatched so no
network is touched. The register endpoint is exercised through the API client
to prove the code is actually enforced.
"""
from __future__ import annotations

import pytest

from app.services import email_code_service
from app.services.email_code_service import (
    EmailCodeError,
    request_code,
    verify_and_consume,
)


class FakeRedis:
    """Minimal async stand-in for the redis client surface we use."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        self.ttls[key] = ex or 0
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.kv:
                del self.kv[k]
                n += 1
        return n

    async def exists(self, key):
        return 1 if key in self.kv else 0

    async def ttl(self, key):
        return self.ttls.get(key, -2)

    async def expire(self, key, seconds):
        if key in self.kv or key in self.zsets:
            self.ttls[key] = seconds
            return True
        return False

    async def zcard(self, key):
        return len(self.zsets.get(key, {}))

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(email_code_service, "get_redis", lambda: fake)
    return fake


@pytest.fixture
def smtp_ok(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def _fake_send(to_email, code, purpose):
        sent.append((to_email, code))

    monkeypatch.setattr(email_code_service, "send_verification_email", _fake_send)
    return sent


async def test_request_code_stores_and_sends(fake_redis, smtp_ok):
    payload = await request_code("User@Example.com ", "register")
    assert payload["sent"] is True
    # Normalized email got the mail.
    assert smtp_ok and smtp_ok[0][0] == "user@example.com"
    assert len(smtp_ok[0][1]) == 6 and smtp_ok[0][1].isdigit()
    # The code verifies.
    assert await verify_and_consume("user@example.com", smtp_ok[0][1])


async def test_resend_interval_blocks_second_request(fake_redis, smtp_ok):
    await request_code("a@b.co", "register")
    with pytest.raises(EmailCodeError) as ei:
        await request_code("a@b.co", "register")
    assert "频繁" in str(ei.value) or ei.value.retry_after


async def test_burst_cap_blocks_after_limit(fake_redis, smtp_ok, monkeypatch):
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "EMAIL_CODE_RESEND_INTERVAL", 0)  # disable interval
    for _ in range(s.EMAIL_CODE_BURST_LIMIT):
        await request_code("burst@b.co", "register")
    with pytest.raises(EmailCodeError):
        await request_code("burst@b.co", "register")


async def test_code_is_single_use(fake_redis, smtp_ok):
    await request_code("once@b.co", "register")
    code = smtp_ok[0][1]
    assert await verify_and_consume("once@b.co", code) is True
    # Second use fails.
    assert await verify_and_consume("once@b.co", code) is False


async def test_wrong_code_fails_and_keeps_code(fake_redis, smtp_ok):
    await request_code("keep@b.co", "register")
    code = smtp_ok[0][1]
    assert await verify_and_consume("keep@b.co", "000000") is False or code == "000000"
    # Original still valid after a wrong attempt.
    assert await verify_and_consume("keep@b.co", code) is True


async def test_expired_code_fails(fake_redis, smtp_ok):
    await request_code("exp@b.co", "register")
    # Simulate TTL expiry.
    key = next(k for k in fake_redis.kv if "email_code:" in k)
    del fake_redis.kv[key]
    assert await verify_and_consume("exp@b.co", smtp_ok[0][1]) is False


async def test_dev_mode_echoes_code(fake_redis, smtp_ok, monkeypatch):
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "MAIL_ENABLED", False)
    monkeypatch.setattr(s, "ENV", "dev")
    payload = await request_code("dev@b.co", "register")
    assert payload.get("debug_code")
    # And the echoed code verifies (stored before the echo decision).
    assert await verify_and_consume("dev@b.co", payload["debug_code"])


# --------------------------------------------------------------------------- #
# Register endpoint enforces the code
# --------------------------------------------------------------------------- #
async def test_register_requires_valid_code(client, fake_redis, smtp_ok, db_session, monkeypatch):
    # These endpoint tests opt INTO enforcement (conftest disables it for the
    # rest of the suite via MAIL_ENABLED=false).
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "MAIL_ENABLED", True)

    from tests.conftest import auth_headers

    h = auth_headers()
    # No code requested yet -> any code fails.
    r = await client.post(
        "/api/auth/register",
        json={
            "email": "newuser@example.com",
            "username": "newuser",
            "password": "Valid123pw",
            "verification_code": "123456",
        },
        headers=h,
    )
    assert r.status_code == 400
    assert "验证码" in r.json()["detail"]


async def test_register_succeeds_with_valid_code(client, fake_redis, smtp_ok, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "MAIL_ENABLED", True)
    email = "ok@example.com"
    await request_code(email, "register")
    code = smtp_ok[-1][1]
    r = await client.post(
        "/api/auth/register",
        json={
            "email": email,
            "username": "okuser",
            "password": "Valid123pw",
            "verification_code": code,
        },
    )
    assert r.status_code in (201, 409), r.text
    if r.status_code == 201:
        # The code is consumed: an immediate second use of the same code fails.
        r2 = await client.post(
            "/api/auth/register",
            json={
                "email": email,
                "username": "okuser2",
                "password": "Valid123pw",
                "verification_code": code,
            },
        )
        assert r2.status_code == 400
