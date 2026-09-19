"""Auth flow: register -> login -> /me, plus duplicate + bad-password guards.

The second half of this file pins token *revocation*: an issued access token has
to carry the jti/ver claims the blacklist and the kill-switch match against, or
logout / 改密 / 管理员停用 are all silently no-ops.
"""
from __future__ import annotations

import uuid

from tests.conftest import auth_headers, get_access_token


async def test_register_login_me_round_trip(client):
    reg = await client.post(
        "/api/auth/register",
        json={"email": "alice@example.com", "username": "alice", "password": "Passw0rd!"},
    )
    assert reg.status_code == 201, reg.text
    token = reg.json()["access_token"]

    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "alice@example.com"


async def test_login_with_correct_password(client):
    await client.post(
        "/api/auth/register",
        json={"email": "bob@example.com", "username": "bob", "password": "Passw0rd!"},
    )
    r = await client.post(
        "/api/auth/login",
        json={"email": "bob@example.com", "password": "Passw0rd!"},
    )
    assert r.status_code == 200
    assert "access_token" in r.json()


async def test_login_bad_password(client):
    await client.post(
        "/api/auth/register",
        json={"email": "carol@example.com", "username": "carol", "password": "Passw0rd!"},
    )
    r = await client.post(
        "/api/auth/login",
        json={"email": "carol@example.com", "password": "wrong-password"},
    )
    assert r.status_code == 401


async def test_duplicate_register_conflicts(client):
    payload = {"email": "dave@example.com", "username": "dave", "password": "Passw0rd!"}
    first = await client.post("/api/auth/register", json=payload)
    assert first.status_code == 201
    second = await client.post("/api/auth/register", json=payload)
    assert second.status_code == 409


# --------------------------------------------------------------------------- #
# 密码策略：注册最短 8 位（与前端 login/page.tsx 同一条线）
# --------------------------------------------------------------------------- #
async def test_register_rejects_a_6_character_password(client, db_session):
    """直连 API 建弱密码的路要堵死：schema 与 validate_password_strength 都得拒。"""
    from sqlalchemy import select

    from app.models import User

    r = await client.post(
        "/api/auth/register",
        json={"email": "weakpw@example.com", "username": "weakpw", "password": "Ab1cde"},
    )
    assert r.status_code in (400, 422), r.text
    # 账号不能被建出来。按自己的用户名过滤：测试库是单连接共享的，
    # count(*) 会把别人用例的用户算进来。
    row = (
        await db_session.execute(select(User).where(User.username == "weakpw"))
    ).first()
    assert row is None


async def test_register_accepts_exactly_eight_characters(client):
    r = await client.post(
        "/api/auth/register",
        json={"email": "eight@example.com", "username": "eight", "password": "Ab1cdefg"},
    )
    assert r.status_code == 201, r.text


# --------------------------------------------------------------------------- #
# Token 吊销：签发 -> bump/登出 -> 401
# --------------------------------------------------------------------------- #
async def _register(client, email: str, username: str, password: str = "Passw0rd!"):
    reg = await client.post(
        "/api/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    assert reg.status_code == 201, reg.text
    return reg


async def test_issued_tokens_carry_jti_and_ver(client):
    """不带 jti/ver 的 access token 让登出与 token_version 闸门全部失效。"""
    from app.core.security import decode_token

    reg = await _register(client, "claims@example.com", "claims")
    access = decode_token(reg.json()["access_token"])
    assert access.get("jti"), access
    assert access.get("ver") == 0

    # refresh token 也要带：否则 bump 版本后用旧 refresh 刷新一次就续了命。
    refresh_tok = reg.cookies.get("refresh_token")
    assert refresh_tok
    refresh = decode_token(refresh_tok)
    assert refresh.get("jti")
    assert refresh.get("ver") == 0


async def test_refresh_issues_a_revocable_access_token(client):
    from app.core.security import decode_token

    reg = await _register(client, "rot@example.com", "rotator")
    refresh_tok = reg.cookies.get("refresh_token")
    r = await client.post(
        "/api/auth/refresh", headers={"cookie": f"refresh_token={refresh_tok}"}
    )
    assert r.status_code == 200, r.text
    payload = decode_token(r.json()["access_token"])
    assert payload.get("jti") and payload.get("ver") == 0
    # 轮转：被消费掉的旧 refresh token 不能再换一个 access token。
    replay = await client.post(
        "/api/auth/refresh", headers={"cookie": f"refresh_token={refresh_tok}"}
    )
    assert replay.status_code == 401


async def test_bumping_token_version_revokes_access_and_refresh(client, db_session):
    """管理员停用 / 改密都只 bump 这一列，两条签发路径必须同时认它。"""
    from app.models import User

    reg = await _register(client, "kill@example.com", "killswitch")
    user_id = uuid.UUID(reg.json()["user"]["id"])
    token = reg.json()["access_token"]
    assert (await client.get("/api/auth/me", headers=auth_headers(token))).status_code == 200

    user = await db_session.get(User, user_id)
    assert user is not None
    user.token_version = int(user.token_version or 0) + 1
    await db_session.commit()

    me = await client.get("/api/auth/me", headers=auth_headers(token))
    assert me.status_code == 401
    refreshed = await client.post(
        "/api/auth/refresh",
        headers={"cookie": f"refresh_token={reg.cookies.get('refresh_token')}"},
    )
    assert refreshed.status_code == 401


async def test_legacy_token_without_ver_still_works(client):
    """签发口径收紧前已发出去的 token 只有 30 分钟寿命，不能当场打死全站。"""
    token = get_access_token()  # conftest 造的裸 token：无 jti、无 ver
    assert (await client.get("/api/auth/me", headers=auth_headers(token))).status_code == 200


async def test_logout_blacklists_the_access_token(client):
    reg = await _register(client, "out@example.com", "logoutuser")
    token = reg.json()["access_token"]
    refresh_tok = reg.cookies.get("refresh_token")

    out = await client.post(
        "/api/auth/logout",
        headers={**auth_headers(token), "cookie": f"refresh_token={refresh_tok}"},
    )
    assert out.status_code == 204

    # 登出前这是个好 token；登出后 access 与 refresh 都必须失效。
    assert (await client.get("/api/auth/me", headers=auth_headers(token))).status_code == 401
    refreshed = await client.post(
        "/api/auth/refresh", headers={"cookie": f"refresh_token={refresh_tok}"}
    )
    assert refreshed.status_code == 401


async def test_logout_without_credentials_is_still_idempotent(client):
    assert (await client.post("/api/auth/logout")).status_code == 204
