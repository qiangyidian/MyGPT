"""改密 / 找回密码，以及「微信账号从未设置密码」的状态判断。

Redis 用 tests/test_email_codes.py 里那个内存假客户端，SMTP 用 monkeypatch 记录
收件人，所以这些用例不碰网络。所有断言按自己的 user_id / username / email 过滤：
测试库是 StaticPool 单连接共享的，全局计数会把别的用例算进来。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.security import PASSWORD_NOT_SET, decode_token, hash_password, verify_password
from app.models import User
from app.services import auth_service, email_code_service
from tests.conftest import auth_headers, get_access_token
from tests.test_email_codes import FakeRedis


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(email_code_service, "get_redis", lambda: fake)
    return fake


@pytest.fixture
def mailed(monkeypatch):
    """Capture (email, code, purpose) instead of sending."""
    sent: list[tuple[str, str, str]] = []

    async def _fake_send(to_email, code, purpose):
        sent.append((to_email, code, purpose))

    monkeypatch.setattr(email_code_service, "send_verification_email", _fake_send)
    return sent


async def _add_user(
    db, *, email, username, password=None, password_hash=None, is_active=True
):
    user = User(
        email=email,
        username=username,
        password_hash=password_hash or hash_password(password),
        is_active=is_active,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _reload(db, user_id):
    """从库里重读这一行（populate_existing 覆盖身份映射里的旧值）。

    端点用的是另一个 session，测试 session 里的实例是改密前的快照；用
    ``expire_all()`` 会让属性访问在同步上下文里触发懒加载（MissingGreenlet）。
    """
    row = await db.execute(
        select(User).where(User.id == user_id).execution_options(populate_existing=True)
    )
    return row.scalar_one()


# --------------------------------------------------------------------------- #
# 状态判断：这个账号有没有「原密码」可校验
# --------------------------------------------------------------------------- #
def test_password_is_set_matrix():
    assert auth_service.password_is_set(_row(PASSWORD_NOT_SET)) is False
    assert auth_service.password_is_set(_row("")) is False
    assert auth_service.password_is_set(_row("   ")) is False
    assert auth_service.password_is_set(_row("x")) is False  # 占位/脏数据：谁也验不过
    assert auth_service.password_is_set(_row(hash_password("Passw0rd!"))) is True


def _row(password_hash: str) -> User:
    # 未持久化的实例就够用：password_is_set 只看这一列。
    return User(email="who@example.com", username="who", password_hash=password_hash)


# --------------------------------------------------------------------------- #
# 改密
# --------------------------------------------------------------------------- #
async def test_change_password_swaps_credentials_and_kills_old_tokens(client):
    reg = await client.post(
        "/api/auth/register",
        json={"email": "cp1@example.com", "username": "cp1", "password": "OldPass123"},
    )
    assert reg.status_code == 201, reg.text
    token = reg.json()["access_token"]

    # 有密码的账号想改密必须带原密码，少传字段不是「免校验」的入口。
    missing = await client.post(
        "/api/auth/password", json={"new_password": "NewPass123"}, headers=auth_headers(token)
    )
    assert missing.status_code == 401

    wrong = await client.post(
        "/api/auth/password",
        json={"old_password": "WrongPass123", "new_password": "NewPass123"},
        headers=auth_headers(token),
    )
    assert wrong.status_code == 401
    # 猜原密码猜错了不能顺手把版本 bump 掉——那等于给别人提供拒绝服务开关。
    assert (await client.get("/api/auth/me", headers=auth_headers(token))).status_code == 200

    ok = await client.post(
        "/api/auth/password",
        json={"old_password": "OldPass123", "new_password": "NewPass123"},
        headers=auth_headers(token),
    )
    assert ok.status_code == 200, ok.text
    new_token = ok.json()["access_token"]
    assert decode_token(new_token)["ver"] == 1

    assert (await client.get("/api/auth/me", headers=auth_headers(token))).status_code == 401
    assert (await client.get("/api/auth/me", headers=auth_headers(new_token))).status_code == 200

    old_login = await client.post(
        "/api/auth/login", json={"email": "cp1@example.com", "password": "OldPass123"}
    )
    assert old_login.status_code == 401
    new_login = await client.post(
        "/api/auth/login", json={"email": "cp1@example.com", "password": "NewPass123"}
    )
    assert new_login.status_code == 200


async def test_change_password_requires_authentication(client):
    r = await client.post(
        "/api/auth/password", json={"old_password": "OldPass123", "new_password": "NewPass123"}
    )
    assert r.status_code == 401


async def test_change_password_rejects_a_weak_new_password(client, db_session):
    user = await _add_user(
        db_session, email="weak2@example.com", username="weak2", password="OldPass123"
    )
    r = await client.post(
        "/api/auth/password",
        json={"old_password": "OldPass123", "new_password": "Ab1cde"},
        headers=auth_headers(get_access_token(user.id)),
    )
    assert r.status_code in (400, 422), r.text
    fresh = await _reload(db_session, user.id)
    assert verify_password("OldPass123", fresh.password_hash)
    assert int(fresh.token_version or 0) == 0


async def test_login_still_accepts_a_legacy_short_password(client, db_session):
    """8 位只约束注册/改密/重置。历史用户的短密码不能被挡在登录门外。"""
    await _add_user(
        db_session, email="legacy@example.com", username="legacy", password="Ab1c"
    )
    r = await client.post(
        "/api/auth/login", json={"email": "legacy@example.com", "password": "Ab1c"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]


async def test_wechat_account_without_password_can_set_one(client, db_session):
    """扫码自动注册的账号从未设置密码：免校验原密码直接设置，而不是永久挡死。"""
    openid = "oPWDNOTSET00000000000000001"
    user = await _add_user(
        db_session,
        email=f"wx_{openid}@wechat.local",
        username=f"wxpw{openid[-5:]}",
        password_hash=PASSWORD_NOT_SET,
    )
    r = await client.post(
        "/api/auth/password",
        json={"new_password": "WxPass123"},
        headers=auth_headers(get_access_token(user.id)),
    )
    assert r.status_code == 200, r.text

    fresh = await _reload(db_session, user.id)
    assert verify_password("WxPass123", fresh.password_hash)
    assert int(fresh.token_version or 0) == 1

    # 设过密码之后这个账号就是普通账号：再改密必须带原密码。
    again = await client.post(
        "/api/auth/password",
        json={"new_password": "SecondPass123"},
        headers=auth_headers(r.json()["access_token"]),
    )
    assert again.status_code == 401
    with_old = await client.post(
        "/api/auth/password",
        json={"old_password": "WxPass123", "new_password": "SecondPass123"},
        headers=auth_headers(r.json()["access_token"]),
    )
    assert with_old.status_code == 200


# --------------------------------------------------------------------------- #
# 找回密码
# --------------------------------------------------------------------------- #
async def test_forgot_then_reset_flow(client, fake_redis, mailed, db_session):
    reg = await client.post(
        "/api/auth/register",
        json={"email": "forgot1@example.com", "username": "forgot1", "password": "OldPass123"},
    )
    assert reg.status_code == 201, reg.text
    token = reg.json()["access_token"]

    forgot = await client.post(
        "/api/auth/password/forgot", json={"email": "forgot1@example.com"}
    )
    assert forgot.status_code == 200, forgot.text
    # 非生产环境也不回显重置码：它是直接改写密码的凭据。
    assert "debug_code" not in forgot.json()
    assert mailed and mailed[-1] == ("forgot1@example.com", mailed[-1][1], "reset")
    code = mailed[-1][1]

    bad = await client.post(
        "/api/auth/password/reset",
        json={
            "email": "forgot1@example.com",
            "code": "000000" if code != "000000" else "999999",
            "new_password": "NewPass123",
        },
    )
    assert bad.status_code == 400

    ok = await client.post(
        "/api/auth/password/reset",
        json={"email": "forgot1@example.com", "code": code, "new_password": "NewPass123"},
    )
    assert ok.status_code == 200, ok.text

    # 旧会话（劫持中的那种）随 token_version 一起死。
    assert (await client.get("/api/auth/me", headers=auth_headers(token))).status_code == 401
    old_login = await client.post(
        "/api/auth/login", json={"email": "forgot1@example.com", "password": "OldPass123"}
    )
    assert old_login.status_code == 401
    new_login = await client.post(
        "/api/auth/login", json={"email": "forgot1@example.com", "password": "NewPass123"}
    )
    assert new_login.status_code == 200

    # 验证码单次使用。
    replay = await client.post(
        "/api/auth/password/reset",
        json={"email": "forgot1@example.com", "code": code, "new_password": "ThirdPass123"},
    )
    assert replay.status_code == 400

    fresh = await _reload(db_session, uuid.UUID(reg.json()["user"]["id"]))
    assert verify_password("NewPass123", fresh.password_hash)


async def test_forgot_password_never_reveals_whether_the_email_exists(
    client, fake_redis, mailed
):
    await client.post(
        "/api/auth/register",
        json={"email": "known@example.com", "username": "known", "password": "Passw0rd!"},
    )
    known = await client.post(
        "/api/auth/password/forgot", json={"email": "known@example.com"}
    )
    unknown = await client.post(
        "/api/auth/password/forgot", json={"email": "nobody@example.com"}
    )
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    # 只有真实存在、且邮箱可达的账号才会发信。
    assert [m[0] for m in mailed] == ["known@example.com"]


async def test_forgot_password_never_mails_a_synthetic_address(
    client, fake_redis, mailed, db_session
):
    """合成邮箱（微信自动注册 / 已注销）收不到信：一次都不能发出去。

    邮箱字段是 EmailStr，``@wechat.local`` 这种保留域在进端点前就被判 422 ——
    所以真正兜底的是端点里那道 ``_mail_deliverable`` 判断：哪天入参放宽，
    它也得拦住「往合成域名发信」。这里两头都钉住。
    """
    from app.api.auth import _mail_deliverable

    assert _mail_deliverable("wx_oSKIPPED0000000000000001@wechat.local") is False
    assert _mail_deliverable("deleted-abcd@deleted.invalid") is False
    assert _mail_deliverable("someone@example.com") is True

    await _add_user(
        db_session,
        email="wx_oSKIPPED0000000000000001@wechat.local",
        username="wxskip",
        password_hash=PASSWORD_NOT_SET,
    )
    r = await client.post(
        "/api/auth/password/forgot",
        json={"email": "wx_oSKIPPED0000000000000001@wechat.local"},
    )
    # 422（EmailStr 直接拒）或 200（通用文案）都不泄露账号是否存在，但都不能发信。
    assert r.status_code in (200, 422), r.text
    assert mailed == []


async def test_a_registration_code_cannot_be_used_to_reset_a_password(
    client, fake_redis, mailed
):
    """purpose 不同 → Redis 键不同：注册码换不来改密。"""
    await client.post(
        "/api/auth/register",
        json={"email": "iso@example.com", "username": "iso", "password": "Passw0rd!"},
    )
    await email_code_service.request_code("iso@example.com", purpose="register")
    code = mailed[-1][1]
    r = await client.post(
        "/api/auth/password/reset",
        json={"email": "iso@example.com", "code": code, "new_password": "NewPass123"},
    )
    assert r.status_code == 400


async def test_reset_requires_a_valid_code_even_without_smtp(client, fake_redis, mailed):
    """MAIL_ENABLED=false 的 dev/test 也不能空码改密——那是任意账号的接管口。"""
    from app.core.config import get_settings

    assert get_settings().MAIL_ENABLED is False
    await client.post(
        "/api/auth/register",
        json={"email": "nocd@example.com", "username": "nocd", "password": "Passw0rd!"},
    )
    r = await client.post(
        "/api/auth/password/reset",
        json={"email": "nocd@example.com", "code": "123456", "new_password": "NewPass123"},
    )
    assert r.status_code == 400


async def test_reset_rejects_a_weak_new_password(client, fake_redis, mailed, db_session):
    user = await _add_user(
        db_session, email="weakr@example.com", username="weakr", password="Passw0rd!"
    )
    await client.post("/api/auth/password/forgot", json={"email": "weakr@example.com"})
    code = mailed[-1][1]
    r = await client.post(
        "/api/auth/password/reset",
        json={"email": "weakr@example.com", "code": code, "new_password": "Ab1cde"},
    )
    assert r.status_code in (400, 422), r.text
    fresh = await _reload(db_session, user.id)
    assert verify_password("Passw0rd!", fresh.password_hash)


async def test_reset_of_a_disabled_account_is_refused(client, fake_redis, mailed, db_session):
    await _add_user(
        db_session,
        email="disabledpw@example.com",
        username="disabledpw",
        password="Passw0rd!",
        is_active=False,
    )
    await client.post("/api/auth/password/forgot", json={"email": "disabledpw@example.com"})
    code = mailed[-1][1]
    r = await client.post(
        "/api/auth/password/reset",
        json={"email": "disabledpw@example.com", "code": code, "new_password": "NewPass123"},
    )
    assert r.status_code == 401


async def test_email_code_purposes_are_isolated_in_redis(fake_redis, mailed, monkeypatch):
    """同一地址的注册码与重置码互不覆盖，也互不消费。

    重发间隔是「按地址共享」的（防轰炸预算不该按 purpose 翻倍），所以这里显式
    关掉它，只验 purpose 隔离本身。
    """
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "EMAIL_CODE_RESEND_INTERVAL", 0)

    await email_code_service.request_code("both@b.co", purpose="register")
    register_code = mailed[-1][1]
    await email_code_service.request_code("both@b.co", purpose="reset")
    reset_code = mailed[-1][1]

    assert await email_code_service.verify_and_consume("both@b.co", reset_code, "reset")
    assert not await email_code_service.verify_and_consume("both@b.co", reset_code, "reset")
    # 注册码还在原地：它没有被重置流程清掉，也没有变成重置凭据。
    assert await email_code_service.verify_and_consume("both@b.co", register_code, "register")
