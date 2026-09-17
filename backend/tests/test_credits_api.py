"""积分 API 测试：权限、错误码、兑换全链路。

**余额断言一律用全新注册的用户**，不要用种子用户。整个测试套件共用同一个
内存库（session 级的 ``seeded_db``），对种子用户做绝对余额断言会随执行顺序
变化而失败 —— 那是假阳性，会耗掉排查时间。
"""
from __future__ import annotations

import uuid


from tests.conftest import auth_headers


async def _fresh_user(client) -> tuple[dict[str, str], dict]:
    """注册一个全新用户，返回 ``(请求头, 用户对象)``。

    用户对象来自注册响应的 ``user`` 字段，含 ``id`` / ``email`` / ``username``。
    """
    suffix = uuid.uuid4().hex[:8]
    res = await client.post(
        "/api/auth/register",
        json={
            "email": f"u{suffix}@example.com",
            "username": f"u{suffix}",
            "password": "FreshPass123",
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    return {"Authorization": f"Bearer {body['access_token']}"}, body["user"]


async def _create_batch(client, admin_token, count=3, credits=500, **over):
    body = {"name": "API 批次", "credits_per_code": credits, "count": count, **over}
    res = await client.post(
        "/api/admin/redeem-batches", json=body, headers=auth_headers(admin_token)
    )
    assert res.status_code == 200, res.text
    return res.json()


async def test_me_reports_zero_balance_for_a_new_user(client):
    headers, _ = await _fresh_user(client)
    res = await client.get("/api/credits/me", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["balance"] == 0
    assert data["lifetime_granted"] == 0
    assert data["lifetime_consumed"] == 0
    assert data["enforced"] is False  # ENV=test 强制关闭


async def test_me_requires_auth(client):
    assert (await client.get("/api/credits/me")).status_code == 401


async def test_redeem_adds_credits_and_balance_reflects_it(client, admin_token):
    headers, _ = await _fresh_user(client)
    created = await _create_batch(client, admin_token, count=1, credits=750)
    code = created["codes"][0]

    res = await client.post("/api/credits/redeem", json={"code": code}, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["credits_added"] == 750
    assert body["balance"] == 750

    me = (await client.get("/api/credits/me", headers=headers)).json()
    assert me["balance"] == 750
    assert me["lifetime_granted"] == 750


async def test_redeem_twice_returns_409_with_code(client, admin_token):
    headers, _ = await _fresh_user(client)
    created = await _create_batch(client, admin_token, count=1, credits=100)
    code = created["codes"][0]
    await client.post("/api/credits/redeem", json={"code": code}, headers=headers)

    res = await client.post("/api/credits/redeem", json={"code": code}, headers=headers)
    assert res.status_code == 409
    assert res.json()["code"] == "redeem_code_used"


async def test_redeem_unknown_code_returns_404_with_code(client):
    headers, _ = await _fresh_user(client)
    res = await client.post(
        "/api/credits/redeem", json={"code": "ZZZZ-ZZZZ-ZZZZ-ZZZZ"}, headers=headers
    )
    assert res.status_code == 404
    assert res.json()["code"] == "redeem_code_not_found"


async def test_redeem_requires_auth(client):
    res = await client.post("/api/credits/redeem", json={"code": "X"})
    assert res.status_code == 401


async def test_ledger_lists_entries_newest_first(client, admin_token):
    headers, _ = await _fresh_user(client)
    created = await _create_batch(client, admin_token, count=2, credits=300)
    for code in created["codes"]:
        await client.post("/api/credits/redeem", json={"code": code}, headers=headers)

    body = (await client.get("/api/credits/ledger", headers=headers)).json()
    entries = body["entries"]
    assert len(entries) == 2
    assert entries[0]["delta"] == 300
    assert entries[0]["balance_after"] == 600  # 最新一条在后面那条之后
    assert entries[0]["reason"] == "redeem"
    assert entries[1]["balance_after"] == 300
    assert body["next_cursor"] is None


async def test_ledger_paginates_without_overlap(client, admin_token):
    headers, _ = await _fresh_user(client)
    created = await _create_batch(client, admin_token, count=5, credits=10)
    for code in created["codes"]:
        await client.post("/api/credits/redeem", json={"code": code}, headers=headers)

    page1 = (await client.get("/api/credits/ledger?limit=2", headers=headers)).json()
    assert len(page1["entries"]) == 2
    assert page1["next_cursor"]

    page2 = (
        await client.get(
            f"/api/credits/ledger?limit=2&cursor={page1['next_cursor']}", headers=headers
        )
    ).json()
    ids = {e["id"] for e in page1["entries"]} | {e["id"] for e in page2["entries"]}
    assert len(ids) == 4  # 无重叠


# ---- 管理端 --------------------------------------------------------------- #

async def test_admin_endpoints_reject_non_admin(client, auth_token, admin_token):
    # 建一个真实批次，让批次级路由（codes / void）也进这个守卫循环。
    created = await _create_batch(client, admin_token, count=1, credits=10)
    batch_id = created["batch"]["id"]
    for method, path in [
        ("post", "/api/admin/redeem-batches"),
        ("get", "/api/admin/redeem-batches"),
        ("get", f"/api/admin/redeem-batches/{batch_id}/codes"),
        ("post", f"/api/admin/redeem-batches/{batch_id}/void"),
        ("get", "/api/admin/credits/accounts"),
        ("get", f"/api/admin/credits/ledger?user_id={uuid.uuid4()}"),
        ("post", "/api/admin/credits/adjust"),
    ]:
        res = await getattr(client, method)(path, headers=auth_headers(auth_token))
        assert res.status_code == 403, f"{method} {path} -> {res.status_code}"


async def test_create_batch_returns_plaintext_codes_once(client, admin_token):
    created = await _create_batch(client, admin_token, count=3, credits=200)
    assert len(created["codes"]) == 3

    # 列表接口不再返回明文，只有前缀
    listed = (
        await client.get("/api/admin/redeem-batches", headers=auth_headers(admin_token))
    ).json()
    target = next(b for b in listed if b["batch"]["id"] == created["batch"]["id"])
    assert target["total"] == 3
    assert target["active"] == 3
    assert target["redeemed"] == 0

    codes = (
        await client.get(
            f"/api/admin/redeem-batches/{created['batch']['id']}/codes",
            headers=auth_headers(admin_token),
        )
    ).json()
    assert len(codes) == 3
    prefixes = {c["code_prefix"] for c in codes}
    assert prefixes == {c.replace("-", "")[:6] for c in created["codes"]}
    assert all("code_hash" not in c for c in codes)


async def test_create_batch_validates_credits(client, admin_token):
    res = await client.post(
        "/api/admin/redeem-batches",
        json={"name": "bad", "credits_per_code": 0, "count": 1},
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 400
    assert res.json()["code"] == "redeem_batch_invalid_credits"


async def test_void_batch_endpoint(client, admin_token):
    created = await _create_batch(client, admin_token, count=3, credits=100)
    res = await client.post(
        f"/api/admin/redeem-batches/{created['batch']['id']}/void",
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 200
    assert res.json()["voided"] == 3


async def test_admin_can_adjust_user_credits(client, admin_token):
    user_headers, user = await _fresh_user(client)

    res = await client.post(
        "/api/admin/credits/adjust",
        json={"user_id": user["id"], "delta": 1234, "note": "客服补偿"},
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 200, res.text
    assert res.json()["balance"] == 1234

    me = (await client.get("/api/credits/me", headers=user_headers)).json()
    assert me["balance"] == 1234


async def test_admin_can_deduct_with_a_negative_delta(client, admin_token):
    user_headers, user = await _fresh_user(client)
    admin_h = auth_headers(admin_token)
    await client.post(
        "/api/admin/credits/adjust",
        json={"user_id": user["id"], "delta": 1000},
        headers=admin_h,
    )
    res = await client.post(
        "/api/admin/credits/adjust",
        json={"user_id": user["id"], "delta": -300},
        headers=admin_h,
    )
    assert res.status_code == 200, res.text
    assert res.json()["balance"] == 700
    assert res.json()["lifetime_consumed"] == 300


async def test_adjust_rejects_zero_delta(client, admin_token):
    _, user = await _fresh_user(client)
    res = await client.post(
        "/api/admin/credits/adjust",
        json={"user_id": user["id"], "delta": 0},
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 400
    assert res.json()["code"] == "credit_adjust_zero"


async def test_adjust_unknown_user_returns_404(client, admin_token):
    res = await client.post(
        "/api/admin/credits/adjust",
        json={"user_id": str(uuid.uuid4()), "delta": 10},
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 404
    assert res.json()["code"] == "user_not_found"


async def test_adjust_emits_an_audit_event(client, admin_token, monkeypatch):
    """调分必须留审计 —— 这是它区别于直接改库的全部意义。

    这里断言的是**路由确实调用了 audit_service.log 且参数正确**，而不是去
    查 audit_events 表。原因：`audit_service.log` 用自己的会话
    （`app.db.AsyncSessionLocal`），而测试库是 conftest 用 StaticPool 另建的
    内存 SQLite，两者不是同一个库（且 `AUTO_CREATE_TABLES=false`，那个库里
    根本没有表）。log() 会静默吞掉失败，所以查表只会得到一个假的失败。
    """
    from app.services import audit_service

    _, user = await _fresh_user(client)

    calls: list[dict] = []

    async def _spy(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(audit_service, "log", _spy)

    await client.post(
        "/api/admin/credits/adjust",
        json={"user_id": user["id"], "delta": 77, "note": "测试审计"},
        headers=auth_headers(admin_token),
    )

    assert calls, "调分没有写审计事件"
    assert calls[0]["action"] == "credits:adjust"
    assert calls[0]["target"] == user["id"]
    assert calls[0]["detail"]["delta"] == 77
    assert calls[0]["detail"]["note"] == "测试审计"


async def test_void_batch_emits_an_audit_event(client, admin_token, monkeypatch):
    """作废是破坏性操作，同样必须留审计。"""
    from app.services import audit_service

    created = await _create_batch(client, admin_token, count=2, credits=100)

    calls: list[dict] = []

    async def _spy(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(audit_service, "log", _spy)

    await client.post(
        f"/api/admin/redeem-batches/{created['batch']['id']}/void",
        headers=auth_headers(admin_token),
    )

    assert calls and calls[0]["action"] == "credits:void_batch"
    assert calls[0]["detail"]["voided"] == 2


async def test_admin_accounts_listing_shows_the_users_balance(client, admin_token):
    _, user = await _fresh_user(client)
    await client.post(
        "/api/admin/credits/adjust",
        json={"user_id": user["id"], "delta": 55},
        headers=auth_headers(admin_token),
    )
    rows = (
        await client.get("/api/admin/credits/accounts", headers=auth_headers(admin_token))
    ).json()
    target = next(r for r in rows if r["user_id"] == user["id"])
    assert target["balance"] == 55


async def test_admin_can_search_accounts_by_email(client, admin_token):
    _, user = await _fresh_user(client)
    rows = (
        await client.get(
            f"/api/admin/credits/accounts?search={user['email']}",
            headers=auth_headers(admin_token),
        )
    ).json()
    assert len(rows) == 1
    assert rows[0]["user_id"] == user["id"]


async def test_admin_can_view_another_users_ledger(client, admin_token):
    """计费争议的关键证据：管理员必须能看到别的用户的流水行。"""
    user_headers, user = await _fresh_user(client)
    admin_h = auth_headers(admin_token)
    await client.post(
        "/api/admin/credits/adjust",
        json={"user_id": user["id"], "delta": 500, "note": "争议排查"},
        headers=admin_h,
    )

    res = await client.get(
        f"/api/admin/credits/ledger?user_id={user['id']}", headers=admin_h
    )
    assert res.status_code == 200, res.text
    entries = res.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["delta"] == 500
    assert entries[0]["reason"] == "admin_adjust"
    assert entries[0]["note"] == "争议排查"


async def test_admin_ledger_unknown_user_returns_404(client, admin_token):
    res = await client.get(
        f"/api/admin/credits/ledger?user_id={uuid.uuid4()}",
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 404
    assert res.json()["code"] == "user_not_found"


async def test_admin_ledger_hides_other_users_rows(client, admin_token):
    """不带 user_id 参数 / 带别人的 id —— 分页只含目标用户的行。"""
    headers, user = await _fresh_user(client)
    await client.post(
        "/api/admin/credits/adjust",
        json={"user_id": user["id"], "delta": 300},
        headers=auth_headers(admin_token),
    )
    _, other = await _fresh_user(client)
    body = (
        await client.get(
            f"/api/admin/credits/ledger?user_id={other['id']}",
            headers=auth_headers(admin_token),
        )
    ).json()
    assert body["entries"] == []
