"""WeChat scan login, after the move to the wechat-auth service.

MyChat no longer derives, issues, throttles or redeems-by-storage: it asks
wechat-auth to redeem a code and gets an openid back. These tests pin the parts
that are still MyChat's — the openid→user mapping and the bind lifecycle — plus
the failure split that matters operationally:

* a code that is wrong/expired/already used/for-another-app → 401
* wechat-auth unreachable, or our secret wrong            → 503

Collapsing the second into the first would tell every user "your code is wrong"
during an outage, and would send whoever is on call looking in the wrong place.

Each test redeems as its OWN openid: the seeded database is session-scoped, so a
shared openid would leak bindings (and one test deactivates its account).
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import User, WechatIdentity
from app.services import wechat_auth_client, wechat_login_service
from app.services.wechat_auth_client import WechatAuthError, WechatAuthUnavailable
from tests.conftest import auth_headers


@pytest.fixture
def redeem_as(monkeypatch):
    """Make every code redeem to ``openid``. Returns the openid."""
    def _set(openid: str) -> str:
        async def _verify(_code: str) -> str:
            return openid

        monkeypatch.setattr(wechat_auth_client, "verify_code", _verify)
        return openid

    return _set


@pytest.fixture
def rejecting(monkeypatch):
    async def _verify(_code: str) -> str:
        raise WechatAuthError("公众号验证码错误或已过期")

    monkeypatch.setattr(wechat_auth_client, "verify_code", _verify)


@pytest.fixture
def unreachable(monkeypatch):
    async def _verify(_code: str) -> str:
        raise WechatAuthUnavailable("微信登录服务暂时不可用，请稍后再试")

    monkeypatch.setattr(wechat_auth_client, "verify_code", _verify)


@pytest.fixture
def wechat_enabled(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "WECHAT_AUTH_ENABLED", True)
    return get_settings()


async def _binding_rows(db, user_id):
    return (
        await db.execute(select(WechatIdentity).where(WechatIdentity.user_id == user_id))
    ).scalars().all()


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
async def test_first_login_auto_registers_and_binds(db_session, redeem_as):
    openid = redeem_as("oAUTO0000000000000000000001")
    user = await wechat_login_service.login_with_code(db_session, "123456")

    assert user.is_active is True
    assert user.role == "user"
    # users requires non-null email / username / password_hash.
    assert user.email == f"wx_{openid}@wechat.local"
    assert user.username.startswith("微信用户")
    assert user.password_hash  # random, unknown to anyone
    identity = (
        await db_session.execute(
            select(WechatIdentity).where(WechatIdentity.openid == openid)
        )
    ).scalar_one()
    assert identity.user_id == user.id


async def test_second_login_returns_the_same_account(db_session, redeem_as):
    redeem_as("oSECOND00000000000000000001")
    first = await wechat_login_service.login_with_code(db_session, "123456")
    second = await wechat_login_service.login_with_code(db_session, "654321")
    assert second.id == first.id


async def test_usernames_do_not_collide_when_openid_tails_match(db_session, redeem_as):
    """Two followers whose openid tails coincide must both be able to register.

    The synthesized username is derived from the openid's last six characters,
    so without a suffix fallback the second insert would violate the unique
    username constraint — surfacing as a 500 on someone's first-ever scan.
    """
    first_openid = "oTAILmatch00000000000000001"
    second_openid = "oOTHERxxxxx0000000000000001"  # same last six
    assert first_openid != second_openid
    assert first_openid[-6:] == second_openid[-6:]

    redeem_as(first_openid)
    first = await wechat_login_service.login_with_code(db_session, "1")
    redeem_as(second_openid)
    second = await wechat_login_service.login_with_code(db_session, "2")

    assert first.username != second.username


async def test_inactive_account_is_refused(db_session, redeem_as):
    openid = redeem_as("oINACTIVE000000000000000001")
    user = await wechat_login_service.login_with_code(db_session, "123456")
    assert user.email == f"wx_{openid}@wechat.local"
    user.is_active = False
    await db_session.commit()

    with pytest.raises(WechatAuthError):
        await wechat_login_service.login_with_code(db_session, "123456")


async def test_binding_links_an_existing_account(db_session, redeem_as):
    redeem_as("oBINDexisting00000000000001")
    existing = User(
        email="binder@example.com", username="binder", password_hash="x",
        role="user", is_active=True,
    )
    db_session.add(existing)
    await db_session.commit()

    await wechat_login_service.bind_openid(db_session, existing, "123456")
    # The next scan logs into the existing account, not a brand-new one.
    assert (await wechat_login_service.login_with_code(db_session, "123456")).id == existing.id


async def test_binding_refuses_an_openid_owned_by_somebody_else(db_session, redeem_as):
    openid = redeem_as("oBINDconflict0000000000001")
    owner = User(
        email="owner@example.com", username="owner", password_hash="x",
        role="user", is_active=True,
    )
    intruder = User(
        email="intruder@example.com", username="intruder", password_hash="x",
        role="user", is_active=True,
    )
    db_session.add_all([owner, intruder])
    await db_session.commit()

    await wechat_login_service.bind_openid(db_session, owner, "123456")
    with pytest.raises(WechatAuthError) as ei:
        await wechat_login_service.bind_openid(db_session, intruder, "123456")
    assert "其他账号" in str(ei.value)

    identity = (
        await db_session.execute(
            select(WechatIdentity).where(WechatIdentity.openid == openid)
        )
    ).scalar_one()
    assert identity.user_id == owner.id


async def test_binding_is_idempotent_for_the_same_account(db_session, redeem_as):
    redeem_as("oBINDidem000000000000000001")
    user = User(
        email="again@example.com", username="again", password_hash="x",
        role="user", is_active=True,
    )
    db_session.add(user)
    await db_session.commit()

    await wechat_login_service.bind_openid(db_session, user, "1")
    await wechat_login_service.bind_openid(db_session, user, "2")
    # Re-binding the same openid must not create a second row (the unique index
    # would eventually reject it).
    assert len(await _binding_rows(db_session, user.id)) == 1


async def test_rebinding_replaces_the_previous_openid(db_session, redeem_as):
    """A user who switches WeChat account must not accumulate bindings."""
    user = User(
        email="swap@example.com", username="swap", password_hash="x",
        role="user", is_active=True,
    )
    db_session.add(user)
    await db_session.commit()

    redeem_as("oSWAPfirst00000000000000001")
    await wechat_login_service.bind_openid(db_session, user, "1")

    second = redeem_as("oSWAPsecond0000000000000001")
    await wechat_login_service.bind_openid(db_session, user, "2")

    assert await wechat_login_service.get_binding(db_session, user) == second
    assert len(await _binding_rows(db_session, user.id)) == 1


async def test_unbind_removes_the_binding(db_session, redeem_as):
    redeem_as("oUNBIND00000000000000000001")
    user = User(
        email="unbind@example.com", username="unbind", password_hash="x",
        role="user", is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await wechat_login_service.bind_openid(db_session, user, "1")

    assert await wechat_login_service.unbind(db_session, user) is True
    assert await wechat_login_service.get_binding(db_session, user) is None
    assert await wechat_login_service.unbind(db_session, user) is False


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
async def test_login_endpoint_issues_a_session(client, wechat_enabled, redeem_as):
    openid = redeem_as("oHTTPlogin00000000000000001")
    r = await client.post("/api/auth/login/wechat", json={"wechat_code": "123456"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"]
    assert body["user"]["email"] == f"wx_{openid}@wechat.local"
    # Refresh token rides in an httponly cookie, as on every other login path.
    assert "refresh_token" in r.cookies


async def test_login_endpoint_rejects_a_bad_code(client, wechat_enabled, rejecting):
    r = await client.post("/api/auth/login/wechat", json={"wechat_code": "000000"})
    assert r.status_code == 401
    assert "验证码" in r.json()["message"]


async def test_login_endpoint_reports_an_outage_distinctly(client, wechat_enabled, unreachable):
    """A dead wechat-auth is a 503, not "your code is wrong"."""
    r = await client.post("/api/auth/login/wechat", json={"wechat_code": "123456"})
    assert r.status_code == 503
    assert "暂时不可用" in r.json()["message"]


async def test_login_endpoint_is_off_when_the_feature_is_disabled(client, rejecting, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "WECHAT_AUTH_ENABLED", False)
    assert (
        await client.post("/api/auth/login/wechat", json={"wechat_code": "1"})
    ).status_code == 503


async def test_binding_round_trip_over_http(client, wechat_enabled, redeem_as):
    openid = redeem_as("oHTTPbind000000000000000001")
    reg = await client.post(
        "/api/auth/register",
        json={"email": "httpbinder@example.com", "username": "httpbinder", "password": "Passw0rd!"},
    )
    headers = auth_headers(reg.json()["access_token"])

    assert (await client.get("/api/auth/wechat/binding", headers=headers)).json()["bound"] is False

    bound = await client.post(
        "/api/auth/wechat/binding", json={"wechat_code": "123456"}, headers=headers
    )
    assert bound.status_code == 200
    assert bound.json() == {"bound": True, "openid": openid}

    after = (await client.get("/api/auth/wechat/binding", headers=headers)).json()
    assert after["bound"] is True and after["openid"] == openid

    assert (await client.delete("/api/auth/wechat/binding", headers=headers)).status_code == 200
    assert (await client.get("/api/auth/wechat/binding", headers=headers)).json()["bound"] is False


async def test_binding_conflict_is_409(client, wechat_enabled, redeem_as):
    redeem_as("oHTTPconflict00000000000001")
    owner = await client.post(
        "/api/auth/register",
        json={"email": "howner@example.com", "username": "howner", "password": "Passw0rd!"},
    )
    intruder = await client.post(
        "/api/auth/register",
        json={"email": "hintruder@example.com", "username": "hintruder", "password": "Passw0rd!"},
    )
    await client.post(
        "/api/auth/wechat/binding",
        json={"wechat_code": "1"},
        headers=auth_headers(owner.json()["access_token"]),
    )
    steal = await client.post(
        "/api/auth/wechat/binding",
        json={"wechat_code": "2"},
        headers=auth_headers(intruder.json()["access_token"]),
    )
    assert steal.status_code == 409


async def test_binding_requires_authentication(client, wechat_enabled):
    assert (
        await client.post("/api/auth/wechat/binding", json={"wechat_code": "1"})
    ).status_code == 401


# --------------------------------------------------------------------------- #
# login-info / qrcode
# --------------------------------------------------------------------------- #
async def test_login_info_degrades_gracefully_when_upstream_is_down(
    client, wechat_enabled, monkeypatch
):
    async def _down():
        return {}

    monkeypatch.setattr(wechat_auth_client, "fetch_login_info", _down)
    body = (await client.get("/api/wechat/login-info")).json()["data"]
    # The page still offers the keyword route and the static account QR.
    assert body["mode"] == "keyword"
    assert body["configured"] is True
    assert body["keyword"]


async def test_login_info_switches_to_qr_mode(client, wechat_enabled, monkeypatch):
    async def _info():
        return {"mode": "qr", "keyword": "mychat", "display_name": "MyChat"}

    monkeypatch.setattr(wechat_auth_client, "fetch_login_info", _info)
    body = (await client.get("/api/wechat/login-info")).json()["data"]
    assert body["mode"] == "qr"
    # Same-origin: the page must not embed another product's hostname.
    assert body["qrcode_url"] == "/api/wechat/qrcode"


async def test_login_info_is_public_and_safe_when_disabled(client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "WECHAT_AUTH_ENABLED", False)
    body = (await client.get("/api/wechat/login-info")).json()["data"]
    assert body["configured"] is False


async def test_qrcode_is_proxied_same_origin(client, wechat_enabled, monkeypatch):
    async def _image():
        return b"\xff\xd8jpegbytes"

    monkeypatch.setattr(wechat_auth_client, "fetch_qr_image", _image)
    r = await client.get("/api/wechat/qrcode")
    assert r.status_code == 200
    assert r.content == b"\xff\xd8jpegbytes"


async def test_qrcode_reports_unavailable_rather_than_serving_a_broken_image(
    client, wechat_enabled, monkeypatch
):
    async def _none():
        return None

    monkeypatch.setattr(wechat_auth_client, "fetch_qr_image", _none)
    assert (await client.get("/api/wechat/qrcode")).status_code == 503
