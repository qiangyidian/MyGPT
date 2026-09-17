"""WeChat Official Account (公众号) scan-to-login.

The load-bearing invariant of this feature is *cross-backend code agreement*:
MyChat and sql2er share one WeChat Official Account, so the same scan produces
the same 6-digit code in both systems. That only works because the code is
DERIVED (HMAC over openid + the message's own CreateTime) rather than random.
The golden vectors below are the shared contract — sql2er carries an identical
table in its own suite, so either implementation drifting fails its own tests.

CreateTime (not server time) is the derivation input on purpose: nginx `mirror`
copies the same XML body to both backends, so both read the same CreateTime and
derive the same code even if the two servers' clocks disagree. Keying on the
message rather than a time window also keeps each request's code distinct, so a
consumed code cannot be brought back by asking again.
"""
from __future__ import annotations

import pytest

from app.core.wechat_mp import (
    build_text_reply,
    derive_login_code,
    parse_wechat_xml,
    sha1_signature,
)

# Shared secret used only by these fixtures (never a real Token).
CONTRACT_TOKEN = "test-token-0123456789abcdef01234567"


# --------------------------------------------------------------------------- #
# derive_login_code — the cross-backend contract
# --------------------------------------------------------------------------- #
# (openid, create_time) -> code, computed from the documented algorithm and
# duplicated verbatim in sql2er's suite. Do not regenerate to make a test pass:
# a mismatch here means the two backends have stopped agreeing, which silently
# breaks scan-to-login for whichever service the user did NOT scan from.
GOLDEN_VECTORS = [
    ("oABC123defGHI456jklMNO789pqr", 1700000000, "095986"),
    ("oABC123defGHI456jklMNO789pqr", 1700000001, "009902"),
    ("oABC123defGHI456jklMNO789pqr", 1700000001, "009902"),
    ("oZZZ000yyyXXX999wwwVVV888uuu", 1700000000, "483441"),
    ("oSAMEopenid0000000000000000", 1700000000, "073685"),
    ("oSAMEopenid0000000000000000", 1700000500, "322942"),
]


@pytest.mark.parametrize("openid,create_time,expected", GOLDEN_VECTORS)
def test_derive_login_code_matches_cross_repo_contract(openid, create_time, expected):
    assert derive_login_code(openid, create_time, CONTRACT_TOKEN) == expected


def test_derive_login_code_is_zero_padded_six_digits():
    # Some vectors are numerically small (e.g. 009902); a raw int would drop the
    # leading zeros and the user could not type what WeChat showed.
    for openid, create_time, _ in GOLDEN_VECTORS:
        code = derive_login_code(openid, create_time, CONTRACT_TOKEN)
        assert len(code) == 6 and code.isdigit()
    assert any(v[2].startswith("0") for v in GOLDEN_VECTORS), "vectors lost their padding case"


def test_derive_login_code_is_stable_for_the_same_message():
    # WeChat re-pushes the identical message when it does not get a reply in 5s.
    # Same CreateTime must derive the same code, or the user would receive a
    # second, different code that also works.
    a = derive_login_code("oABC123defGHI456jklMNO789pqr", 1700000001, CONTRACT_TOKEN)
    b = derive_login_code("oABC123defGHI456jklMNO789pqr", 1700000001, CONTRACT_TOKEN)
    assert a == b


def test_derive_login_code_is_fresh_for_a_new_message():
    # A user who asks again sends a NEW message with a new CreateTime. It must
    # yield a NEW code: otherwise a code that was already consumed would come
    # back to life every time the user re-requested, and a leaked code would
    # work again.
    a = derive_login_code("oABC123defGHI456jklMNO789pqr", 1700000000, CONTRACT_TOKEN)
    b = derive_login_code("oABC123defGHI456jklMNO789pqr", 1700000001, CONTRACT_TOKEN)
    assert a != b


def test_derive_login_code_differs_per_openid():
    a = derive_login_code("oABC123defGHI456jklMNO789pqr", 1700000000, CONTRACT_TOKEN)
    b = derive_login_code("oZZZ000yyyXXX999wwwVVV888uuu", 1700000000, CONTRACT_TOKEN)
    assert a != b


def test_derive_login_code_differs_per_token():
    # The Token is the only secret; a different one must not yield the same code.
    a = derive_login_code("oABC123defGHI456jklMNO789pqr", 1700000000, CONTRACT_TOKEN)
    b = derive_login_code(
        "oABC123defGHI456jklMNO789pqr", 1700000000, "another-token-entirely-0000000"
    )
    assert a != b


# --------------------------------------------------------------------------- #
# Protocol helpers — behaviour pinned against sql2er's implementation, which is
# what the real WeChat backend talks to.
# --------------------------------------------------------------------------- #
def test_sha1_signature_matches_wechat_protocol():
    # Reference value produced by sql2er's app/utils/wechat.py:sha1_signature.
    assert (
        sha1_signature(CONTRACT_TOKEN, "1700000000", "nonce123")
        == "321a71dbf2a77793b6e3359bab75cf6015fbb9ce"
    )


def test_sha1_signature_is_order_independent():
    # WeChat sorts (token, timestamp, nonce) before joining, so argument order
    # must not change the digest.
    a = sha1_signature(CONTRACT_TOKEN, "1700000000", "nonce123")
    assert sha1_signature("1700000000", CONTRACT_TOKEN, "nonce123") == a


def test_parse_wechat_xml_extracts_text_fields():
    body = (
        "<xml><ToUserName><![CDATA[gh_official]]></ToUserName>"
        "<FromUserName><![CDATA[oUSER123]]></FromUserName>"
        "<CreateTime>1700000000</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[验证码]]></Content></xml>"
    )
    parsed = parse_wechat_xml(body)
    assert parsed["FromUserName"] == "oUSER123"
    assert parsed["ToUserName"] == "gh_official"
    assert parsed["CreateTime"] == "1700000000"
    assert parsed["MsgType"] == "text"
    assert parsed["Content"] == "验证码"


def test_parse_wechat_xml_handles_subscribe_event():
    body = (
        "<xml><ToUserName><![CDATA[gh_official]]></ToUserName>"
        "<FromUserName><![CDATA[oNEWUSER]]></FromUserName>"
        "<CreateTime>1700000000</CreateTime>"
        "<MsgType><![CDATA[event]]></MsgType>"
        "<Event><![CDATA[subscribe]]></Event></xml>"
    )
    parsed = parse_wechat_xml(body)
    assert parsed["MsgType"] == "event"
    assert parsed["Event"] == "subscribe"


def test_build_text_reply_is_plain_xml_with_swapped_users():
    reply = build_text_reply("oSENDER", "oOFFICIAL", "验证码：272831")
    # A passive reply goes back TO the sender, FROM the official account.
    assert reply.startswith("<xml>")
    assert "<ToUserName><![CDATA[oSENDER]]></ToUserName>" in reply
    assert "<FromUserName><![CDATA[oOFFICIAL]]></FromUserName>" in reply
    assert "<MsgType><![CDATA[text]]></MsgType>" in reply
    assert "<Content><![CDATA[验证码：272831]]></Content>" in reply
    # Must be raw XML, not JSON: a quoted string makes the WeChat backend reject it.
    assert not reply.strip().startswith('"')


# --------------------------------------------------------------------------- #
# Issue / login service
# --------------------------------------------------------------------------- #
from sqlalchemy import select

from app.models import User
from app.models.wechat_identity import WechatIdentity
from app.services import wechat_mp_service
from app.services.wechat_mp_service import WechatLoginError

SERVICE_TOKEN = "test-token-0123456789abcdef01234567"
CREATE_TIME = 1700000000
OPENID = "oABC123defGHI456jklMNO789pqr"
# What the golden vectors say this (openid, create_time, token) derives to.
EXPECTED_CODE = "095986"


from tests._wechat_fakes import FakeRedis


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(wechat_mp_service, "get_redis", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def wechat_token(monkeypatch):
    """Pin the shared secret so derived codes match the golden vectors."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "WECHAT_MP_TOKEN", SERVICE_TOKEN)
    monkeypatch.setattr(get_settings(), "WECHAT_MP_LOGIN_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(get_settings(), "WECHAT_MP_LOGIN_LOCKOUT_SECONDS", 900)


async def test_issue_login_code_registers_code_for_openid(fake_redis):
    code = await wechat_mp_service.issue_login_code(OPENID, CREATE_TIME)
    assert code == EXPECTED_CODE
    assert fake_redis.kv[f"{wechat_mp_service.CODE_KEY_PREFIX}:{code}"] == OPENID
    # Bounded lifetime: a code left registered forever is a permanent backdoor.
    assert fake_redis.ttls[f"{wechat_mp_service.CODE_KEY_PREFIX}:{code}"] > 0


async def test_issue_login_code_is_idempotent_for_same_openid(fake_redis):
    # WeChat retries the same message; re-issuing must not be treated as a
    # collision and must not change the code the user was already shown.
    first = await wechat_mp_service.issue_login_code(OPENID, CREATE_TIME)
    second = await wechat_mp_service.issue_login_code(OPENID, CREATE_TIME)
    assert first == second == EXPECTED_CODE


async def test_issue_login_code_collision_never_overwrites_mapping(fake_redis, monkeypatch):
    """Two openids deriving the same code must never overwrite each other.

    Overwriting would let the earlier user's code resolve to the later user's
    openid — i.e. typing your own code would log you into someone else's
    account. Fail closed instead: no code handed out, no mapping replaced.
    """
    other_openid = "oZZZ000yyyXXX999wwwVVV888uuu"
    # Pin the derivation so both openids land on the same code.
    monkeypatch.setattr(wechat_mp_service, "derive_login_code", lambda *a, **k: "424242")

    assert await wechat_mp_service.issue_login_code(OPENID, CREATE_TIME) == "424242"
    assert await wechat_mp_service.issue_login_code(other_openid, CREATE_TIME) is None
    # First owner still holds it — no cross-account takeover.
    assert fake_redis.kv[f"{wechat_mp_service.CODE_KEY_PREFIX}:424242"] == OPENID


async def test_login_with_code_auto_registers_and_binds(db_session, fake_redis):
    openid = "oAUTOregister000000000000001"
    code = await wechat_mp_service.issue_login_code(openid, CREATE_TIME)
    user = await wechat_mp_service.login_with_code(db_session, code)

    assert user.is_active is True
    assert user.role == "user"
    # Synthesized columns must satisfy users' NOT NULL + UNIQUE constraints.
    assert user.email == f"wx_{openid}@wechat.local"
    assert user.username.startswith("微信用户")
    assert user.password_hash  # random, unknown to anyone
    # And the openid is bound, which is what makes the NEXT scan return this user.
    identity = (
        await db_session.execute(
            select(WechatIdentity).where(WechatIdentity.openid == openid)
        )
    ).scalar_one()
    assert identity.user_id == user.id


async def test_login_with_code_returns_same_user_for_a_later_scan(db_session, fake_redis):
    openid = "oNEXTscan0000000000000000002"
    first_code = await wechat_mp_service.issue_login_code(openid, CREATE_TIME)
    first = await wechat_mp_service.login_with_code(db_session, first_code)
    # A later scan is a new message, so it derives a different code.
    later = await wechat_mp_service.issue_login_code(openid, CREATE_TIME + 300)
    assert later != first_code
    second = await wechat_mp_service.login_with_code(db_session, later)
    assert second.id == first.id


async def test_login_with_code_is_single_use(db_session, fake_redis):
    code = await wechat_mp_service.issue_login_code("oSINGLEuse000000000000000003", CREATE_TIME)
    await wechat_mp_service.login_with_code(db_session, code)
    with pytest.raises(WechatLoginError):
        await wechat_mp_service.login_with_code(db_session, code)


async def test_login_with_code_rejects_unknown_code(db_session, fake_redis):
    with pytest.raises(WechatLoginError):
        await wechat_mp_service.login_with_code(db_session, "000000")


async def test_login_consumes_code_without_getdel(db_session, fake_redis):
    """Redis < 6.2 has no GETDEL; the fallback must still consume the code."""
    fake_redis.supports_getdel = False
    code = await wechat_mp_service.issue_login_code("oNOgetdel0000000000000000004", CREATE_TIME)
    await wechat_mp_service.login_with_code(db_session, code)
    with pytest.raises(WechatLoginError):
        await wechat_mp_service.login_with_code(db_session, code)


async def test_login_locks_out_after_repeated_failures(db_session, fake_redis):
    """6 digits is a 1e6 space; without a failure cap it can be swept."""
    from app.core.config import get_settings

    limit = get_settings().WECHAT_MP_LOGIN_MAX_ATTEMPTS
    for _ in range(limit):
        with pytest.raises(WechatLoginError):
            await wechat_mp_service.login_with_code(db_session, "000000", ip_address="9.9.9.9")
    # Next attempt is refused by the limiter, not by a code mismatch.
    with pytest.raises(WechatLoginError) as ei:
        await wechat_mp_service.login_with_code(db_session, "000000", ip_address="9.9.9.9")
    assert "次数过多" in str(ei.value)


async def test_lockout_is_checked_before_consuming(db_session, fake_redis):
    """A rate-limited attempt must not burn a real user's pending code.

    If the limit were enforced after consumption, a blocked guess would destroy
    the code the legitimate user is holding — and the one guess that lands
    would sail straight through.
    """
    from app.core.config import get_settings

    code = await wechat_mp_service.issue_login_code("oLOCKout0000000000000000005", CREATE_TIME)
    for _ in range(get_settings().WECHAT_MP_LOGIN_MAX_ATTEMPTS):
        with pytest.raises(WechatLoginError):
            await wechat_mp_service.login_with_code(db_session, "000000", ip_address="8.8.8.8")

    with pytest.raises(WechatLoginError):
        await wechat_mp_service.login_with_code(db_session, code, ip_address="8.8.8.8")
    # Still redeemable from an unthrottled client — it was never consumed.
    user = await wechat_mp_service.login_with_code(db_session, code, ip_address="7.7.7.7")
    assert user is not None


async def test_bind_openid_links_existing_account(db_session, fake_redis):
    openid = "oBINDexisting000000000000006"
    existing = User(
        email="binder@example.com",
        username="binder",
        password_hash="x",
        role="user",
        is_active=True,
    )
    db_session.add(existing)
    await db_session.commit()

    code = await wechat_mp_service.issue_login_code(openid, CREATE_TIME)
    await wechat_mp_service.bind_openid(db_session, existing, code)

    # Next scan logs into the EXISTING account rather than inventing a new one.
    later = await wechat_mp_service.issue_login_code(openid, CREATE_TIME + 300)
    assert (await wechat_mp_service.login_with_code(db_session, later)).id == existing.id


async def test_bind_openid_rejects_openid_owned_by_another_account(db_session, fake_redis):
    openid = "oBINDconflict000000000000007"
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

    await wechat_mp_service.bind_openid(
        db_session, owner, await wechat_mp_service.issue_login_code(openid, CREATE_TIME)
    )
    with pytest.raises(WechatLoginError) as ei:
        await wechat_mp_service.bind_openid(
            db_session,
            intruder,
            await wechat_mp_service.issue_login_code(openid, CREATE_TIME + 300),
        )
    assert "其他账号" in str(ei.value)
    # Ownership is unchanged.
    identity = (
        await db_session.execute(
            select(WechatIdentity).where(WechatIdentity.openid == openid)
        )
    ).scalar_one()
    assert identity.user_id == owner.id


async def test_get_binding_reports_openid_or_none(db_session, fake_redis):
    openid = "oGETbinding00000000000000008"
    user = User(
        email="status@example.com", username="status", password_hash="x",
        role="user", is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    assert await wechat_mp_service.get_binding(db_session, user) is None

    await wechat_mp_service.bind_openid(
        db_session, user, await wechat_mp_service.issue_login_code(openid, CREATE_TIME)
    )
    assert await wechat_mp_service.get_binding(db_session, user) == openid


async def test_unbind_removes_binding(db_session, fake_redis):
    openid = "oUNbind000000000000000000009"
    user = User(
        email="unbind@example.com", username="unbind", password_hash="x",
        role="user", is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await wechat_mp_service.bind_openid(
        db_session, user, await wechat_mp_service.issue_login_code(openid, CREATE_TIME)
    )
    assert await wechat_mp_service.unbind(db_session, user) is True
    assert await wechat_mp_service.get_binding(db_session, user) is None
    # Idempotent-ish: nothing left to remove.
    assert await wechat_mp_service.unbind(db_session, user) is False


async def test_re_requesting_does_not_resurrect_a_consumed_code(db_session, fake_redis):
    """A new message must not make an already-used code valid again.

    This is why the derivation keys on the message's own CreateTime instead of a
    time bucket: with a bucket, every request inside the window would hand back
    the SAME code, so asking again would revive a code that had already been
    consumed — and a leaked code would start working a second time.
    """
    openid = "oRESURRECT00000000000000001"
    first = await wechat_mp_service.issue_login_code(openid, CREATE_TIME)
    assert (await wechat_mp_service.login_with_code(db_session, first)).id

    # The follower asks again: a NEW message, hence a NEW code.
    second = await wechat_mp_service.issue_login_code(openid, CREATE_TIME + 1)
    assert second != first
    # The consumed code stays dead.
    with pytest.raises(WechatLoginError):
        await wechat_mp_service.login_with_code(db_session, first)
    # ...and the fresh one works.
    assert (await wechat_mp_service.login_with_code(db_session, second)).id
