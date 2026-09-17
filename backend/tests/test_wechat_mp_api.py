"""HTTP layer: the WeChat callback, scan login, and binding endpoints."""
from __future__ import annotations

import re

import pytest

from app.core.config import get_settings
from app.core.wechat_mp import derive_login_code
from app.core.wechat_mp import sha1_signature as _sign
from app.services import wechat_mp_service
from tests._wechat_fakes import FakeRedis
from tests.conftest import auth_headers

CALLBACK_TOKEN = "test-token-0123456789abcdef01234567"


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(wechat_mp_service, "get_redis", lambda: fake)
    return fake


@pytest.fixture
def wechat_configured(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "WECHAT_MP_ENABLED", True)
    monkeypatch.setattr(s, "WECHAT_MP_TOKEN", CALLBACK_TOKEN)
    monkeypatch.setattr(s, "WECHAT_MP_KEYWORD", "验证码")
    monkeypatch.setattr(s, "WECHAT_MP_LOGIN_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(s, "WECHAT_MP_LOGIN_LOCKOUT_SECONDS", 900)
    return s


def _signed_params(ts: str = "1700000000", nonce: str = "nonce123") -> str:
    return f"signature={_sign(CALLBACK_TOKEN, ts, nonce)}&timestamp={ts}&nonce={nonce}"


def _message_body(
    *,
    openid: str = "oCALLBACKuser000000000000001",
    create_time: int = 1700000000,
    msg_type: str = "text",
    content: str = "验证码",
    event: str | None = None,
) -> str:
    event_xml = f"<Event><![CDATA[{event}]]></Event>" if event else ""
    return (
        "<xml><ToUserName><![CDATA[gh_official]]></ToUserName>"
        f"<FromUserName><![CDATA[{openid}]]></FromUserName>"
        f"<CreateTime>{create_time}</CreateTime>"
        f"<MsgType><![CDATA[{msg_type}]]></MsgType>{event_xml}"
        f"<Content><![CDATA[{content}]]></Content></xml>"
    )


async def _register(client, email: str, username: str) -> dict:
    r = await client.post(
        "/api/auth/register",
        json={"email": email, "username": username, "password": "Passw0rd!"},
    )
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# login-info (public)
# --------------------------------------------------------------------------- #
async def test_login_info_reports_unconfigured_without_qr(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "WECHAT_MP_LOGIN_QR_URL", "")
    r = await client.get("/api/wechat/login-info")
    assert r.status_code == 200
    body = r.json()["data"]
    # The login page degrades to a text hint rather than a broken image.
    assert body["configured"] is False
    assert body["qrcode_url"] == ""


async def test_login_info_exposes_qr_and_keyword(client, wechat_configured, monkeypatch):
    monkeypatch.setattr(wechat_configured, "WECHAT_MP_LOGIN_QR_URL", "/images/wx.jpg")
    r = await client.get("/api/wechat/login-info")
    assert r.json()["data"] == {
        "configured": True,
        "qrcode_url": "/images/wx.jpg",
        "keyword": "验证码",
    }


# --------------------------------------------------------------------------- #
# callback
# --------------------------------------------------------------------------- #
def _replied_code(xml: str) -> str:
    """The 6-digit code carried by a passive reply.

    Read out of the message body rather than substring-searching the whole
    reply: the XML envelope hardcodes <CreateTime>12345678</CreateTime>, so a
    whole-body search would also "find" a derived code of 123456.
    """
    match = re.search(r"验证码是：(\d{6})", xml)
    assert match, f"reply carries no login code: {xml!r}"
    return match.group(1)


async def test_callback_handshake_returns_plain_echostr(client, wechat_configured):
    """WeChat's handshake compares the body byte-for-byte.

    FastAPI's default serialization returns a QUOTED JSON string, which the
    console rejects outright — so this pins the plain-text response.
    """
    r = await client.get(f"/api/wechat/callback?{_signed_params()}&echostr=hello123")
    assert r.status_code == 200
    assert r.text == "hello123"
    assert not r.text.startswith('"')


async def test_callback_handshake_rejects_bad_signature(client, wechat_configured):
    r = await client.get(
        "/api/wechat/callback?signature=deadbeef&timestamp=1700000000"
        "&nonce=nonce123&echostr=hello123"
    )
    assert r.status_code == 403


async def test_subscribe_event_replies_with_derived_code(client, wechat_configured, fake_redis):
    openid = "oSUBSCRIBEuser00000000000001"
    r = await client.post(
        f"/api/wechat/callback?{_signed_params()}",
        content=_message_body(openid=openid, msg_type="event", event="subscribe", content=""),
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/xml")
    # Raw XML: a quoted JSON string is rejected by the WeChat backend.
    assert r.text.startswith("<xml>")
    # The exact code the OTHER backend derives for the same message — this is
    # what makes one scan work on both services.
    assert _replied_code(r.text) == derive_login_code(openid, 1700000000, CALLBACK_TOKEN)
    # A passive reply is addressed back to the follower.
    assert f"<ToUserName><![CDATA[{openid}]]></ToUserName>" in r.text


async def test_keyword_message_replies_with_code(client, wechat_configured, fake_redis):
    # An already-following user never triggers `subscribe`, so the keyword is
    # the only way for them to get a code.
    openid = "oKEYWORDuser0000000000000001"
    r = await client.post(
        f"/api/wechat/callback?{_signed_params()}",
        content=_message_body(openid=openid, content="验证码"),
    )
    assert _replied_code(r.text) == derive_login_code(openid, 1700000000, CALLBACK_TOKEN)


async def test_login_keyword_always_works(client, wechat_configured, fake_redis):
    openid = "oLOGINkeyword000000000000001"
    r = await client.post(
        f"/api/wechat/callback?{_signed_params()}",
        content=_message_body(openid=openid, content="登录"),
    )
    assert _replied_code(r.text) == derive_login_code(openid, 1700000000, CALLBACK_TOKEN)


async def test_unrelated_text_gets_no_reply(client, wechat_configured, fake_redis):
    r = await client.post(
        f"/api/wechat/callback?{_signed_params()}",
        content=_message_body(content="你好"),
    )
    assert r.status_code == 200
    assert r.text == ""


async def test_callback_post_rejects_bad_signature(client, wechat_configured, fake_redis):
    """The message push carries the same signature as the handshake.

    Trusting the body unverified would let anyone forge a push for any openid
    and mint a login code for that account.
    """
    r = await client.post(
        "/api/wechat/callback?signature=deadbeef&timestamp=1700000000&nonce=nonce123",
        content=_message_body(),
    )
    assert r.status_code == 403


async def test_callback_rejects_oversized_body(client, wechat_configured, fake_redis):
    # Rejected before XML parsing: a public endpoint must not pull an
    # arbitrarily large body into memory.
    r = await client.post(
        f"/api/wechat/callback?{_signed_params()}",
        content=b"<xml>" + b"a" * (70 * 1024) + b"</xml>",
    )
    assert r.status_code == 413


# --------------------------------------------------------------------------- #
# scan login
# --------------------------------------------------------------------------- #
async def test_login_with_code_returns_session(client, wechat_configured, fake_redis):
    openid = "oLOGINsession0000000000000001"
    code = await wechat_mp_service.issue_login_code(openid, 1700000000)
    r = await client.post("/api/auth/login/wechat", json={"wechat_code": code})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"]
    assert body["user"]["email"] == f"wx_{openid}@wechat.local"
    # Refresh token rides in an httponly cookie, same as every other login path.
    assert "refresh_token" in r.cookies


async def test_login_with_bad_code_is_401(client, wechat_configured, fake_redis):
    r = await client.post("/api/auth/login/wechat", json={"wechat_code": "000000"})
    assert r.status_code == 401


async def test_login_is_disabled_when_unconfigured(client, fake_redis, monkeypatch):
    """With the feature off the endpoint must not be a way in."""
    monkeypatch.setattr(get_settings(), "WECHAT_MP_ENABLED", False)
    r = await client.post("/api/auth/login/wechat", json={"wechat_code": "123456"})
    assert r.status_code in (401, 503)


# --------------------------------------------------------------------------- #
# binding
# --------------------------------------------------------------------------- #
async def test_bound_openid_logs_into_the_bound_account(client, wechat_configured, fake_redis):
    """The whole point of the binding flow.

    Without it, an existing account that scans would be handed a brand-new
    empty account instead of logging back into its own.
    """
    openid = "oBINDflow00000000000000000001"
    session = await _register(client, "scanner@example.com", "scanner")

    bind = await client.post(
        "/api/auth/wechat/binding",
        json={"wechat_code": await wechat_mp_service.issue_login_code(openid, 1700000000)},
        headers=auth_headers(session["access_token"]),
    )
    assert bind.status_code == 200, bind.text
    assert bind.json()["openid"] == openid

    # The next scan lands in the SAME account, not a new one.
    login = await client.post(
        "/api/auth/login/wechat",
        json={"wechat_code": await wechat_mp_service.issue_login_code(openid, 1700000300)},
    )
    assert login.status_code == 200, login.text
    assert login.json()["user"]["email"] == "scanner@example.com"


async def test_binding_cannot_steal_an_openid_owned_by_another_account(
    client, wechat_configured, fake_redis
):
    openid = "oBINDsteal0000000000000000001"
    owner = await _register(client, "owner3@example.com", "owner3")
    intruder = await _register(client, "intruder3@example.com", "intruder3")

    await client.post(
        "/api/auth/wechat/binding",
        json={"wechat_code": await wechat_mp_service.issue_login_code(openid, 1700000000)},
        headers=auth_headers(owner["access_token"]),
    )
    steal = await client.post(
        "/api/auth/wechat/binding",
        json={"wechat_code": await wechat_mp_service.issue_login_code(openid, 1700000300)},
        headers=auth_headers(intruder["access_token"]),
    )
    assert steal.status_code == 409

    # Ownership is unchanged: a scan still lands in the owner's account.
    login = await client.post(
        "/api/auth/login/wechat",
        json={"wechat_code": await wechat_mp_service.issue_login_code(openid, 1700000600)},
    )
    assert login.json()["user"]["email"] == "owner3@example.com"


async def test_binding_status_and_unbind(client, wechat_configured, fake_redis):
    openid = "oBINDstatus000000000000000001"
    session = await _register(client, "status2@example.com", "status2")
    headers = auth_headers(session["access_token"])

    assert (await client.get("/api/auth/wechat/binding", headers=headers)).json()["bound"] is False

    await client.post(
        "/api/auth/wechat/binding",
        json={"wechat_code": await wechat_mp_service.issue_login_code(openid, 1700000000)},
        headers=headers,
    )
    after = (await client.get("/api/auth/wechat/binding", headers=headers)).json()
    assert after["bound"] is True
    assert after["openid"] == openid

    assert (await client.delete("/api/auth/wechat/binding", headers=headers)).status_code == 200
    assert (await client.get("/api/auth/wechat/binding", headers=headers)).json()["bound"] is False


async def test_binding_requires_authentication(client, wechat_configured, fake_redis):
    r = await client.post("/api/auth/wechat/binding", json={"wechat_code": "123456"})
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# regression
# --------------------------------------------------------------------------- #
async def test_admin_user_list_survives_synthetic_emails(client, admin_token, db_session):
    """Synthetic emails must not break UserOut serialization.

    Account deletion already writes `deleted-xxx@deleted.invalid`, and WeChat
    signup writes `wx_xxx@wechat.local`. Both are special-use domains that
    pydantic's EmailStr REJECTS, so a single such row used to 500 the admin
    user list for every admin.
    """
    from app.models import User

    db_session.add(
        User(
            email="wx_regression@wechat.local",
            username="wx-regression",
            password_hash="x",
            role="user",
            is_active=True,
        )
    )
    await db_session.commit()

    r = await client.get(
        "/api/admin/users", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert r.status_code == 200, r.text
    assert any(u["email"] == "wx_regression@wechat.local" for u in r.json())
