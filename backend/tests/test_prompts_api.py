"""提示词库 API：CRUD、归属隔离、搜索/分类/分页、PATCH 语义与字段上限。

测试库是 ``Base.metadata.create_all`` 建出来的（迁移不会跑），所以「系统预置模板」
在这里必须自己造一行 ``user_id IS NULL`` 的记录，用完删掉 —— 整个 session 共用一个
内存库，留下一行就会污染后面所有断言。

两条不变量单独钉住：

* **别人的 id 一律 404**（不是 403）：403 等于告诉调用方「这个 id 真实存在」。
* **预置模板可读不可写**（非管理员 403）：它本来就在列表里公开可见，所以这里
  403 不泄露任何信息，而 404 反而会让人误以为自己碰到了一个不存在的 id。
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError

from app.models import PromptTemplate
from app.schemas.prompt_template import (
    CONTENT_MAX,
    DEFAULT_CATEGORY,
    DESCRIPTION_MAX,
    TAG_MAX_COUNT,
    TAG_MAX_LEN,
    TITLE_MAX,
)
from tests.conftest import auth_headers

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _name(label: str) -> str:
    """全局唯一的标题（同一内存库跨用例复用，重名的断言会互相干扰）。"""
    return f"{label} {uuid.uuid4().hex[:8]}"


async def _foreign_headers(client, token: str) -> dict[str, str]:
    """再注册一个用户并拿它的请求头 —— 用来验证跨用户隔离。"""
    reg = await client.post(
        "/api/auth/register",
        json={
            "email": f"prompt-{token}@example.com",
            "username": f"prompt-{token}",
            "password": "Passw0rd!",
        },
    )
    assert reg.status_code in (200, 201), reg.text
    return {"Authorization": f"Bearer {reg.json()['access_token']}"}


async def _create(client, headers, **body) -> dict:
    payload = {"title": _name("模板"), "content": "正文 {{变量}}"} | body
    res = await client.post("/api/prompts", json=payload, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()


async def _list(client, headers, **params) -> list[dict]:
    res = await client.get("/api/prompts", params=params, headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def _ids(rows: list[dict]) -> set[str]:
    return {row["id"] for row in rows}


@pytest_asyncio.fixture
async def preset_template(db_session):
    """一行系统预置模板（迁移 0017 的种子在测试里不会跑，所以手工造）。"""
    row = PromptTemplate(
        user_id=None,  # NULL = 预置
        title=_name("预置模板"),
        content="预置正文 ${变量}",
        category="写作",
        tags=["预置"],
        description="等价于迁移里 bulk_insert 的一行",
        sort_order=5,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    yield row
    await db_session.delete(row)
    await db_session.commit()


# --------------------------------------------------------------------------- #
# CRUD 主流程
# --------------------------------------------------------------------------- #


async def test_prompt_crud_round_trip(client):
    h = auth_headers()
    title, content = _name("周报"), "把要点整理成{{风格}}的周报"
    created = await _create(client, h, title=title, content=content, tags=["办公"])
    pid = created["id"]

    # 归属由服务端决定：客户端传不传 user_id 都不算数。
    assert created["user_id"] == str(_SEEDED_USER)
    assert created["title"] == title and created["content"] == content
    assert created["tags"] == ["办公"]
    assert created["category"] == DEFAULT_CATEGORY
    assert created["sort_order"] == 0  # 用户模板不参与预置排序
    assert created["created_at"] and created["updated_at"]

    detail = await client.get(f"/api/prompts/{pid}", headers=h)
    assert detail.status_code == 200, detail.text
    assert detail.json()["title"] == title

    assert pid in _ids(await _list(client, h))

    deleted = await client.delete(f"/api/prompts/{pid}", headers=h)
    assert deleted.status_code == 204
    assert (await client.get(f"/api/prompts/{pid}", headers=h)).status_code == 404


async def test_list_returns_a_bare_array(client):
    """列表是裸数组（与 /api/models、/api/conversations 一致），前端按数组解析。"""
    res = await client.get("/api/prompts", headers=auth_headers())
    assert res.status_code == 200
    assert isinstance(res.json(), list)


async def test_create_ignores_client_supplied_ownership_fields(client):
    """伪造 ``user_id`` / ``sort_order`` 不会把私人模板变成系统预置。"""
    created = await _create(
        client, auth_headers(), user_id=None, sort_order=99, title=_name("伪装")
    )
    assert created["user_id"] == str(_SEEDED_USER)
    assert created["sort_order"] == 0


# --------------------------------------------------------------------------- #
# 入参清洗与上限（与前端 PROMPT_LIMITS 同源）
# --------------------------------------------------------------------------- #


async def test_create_trims_titles_but_keeps_content_verbatim(client):
    h = auth_headers()
    title = _name("清洗")
    created = await _create(
        client,
        h,
        title=f"  {title}  ",
        content="  正文  ",
        description="   ",
        tags=[" 中文 ", "中文", "", "  ", "术语"],
    )
    assert created["title"] == title
    # 正文一个字符都不许动：模板结尾的换行是内容，不是脏数据。
    assert created["content"] == "  正文  "
    trailing = await _create(client, h, content="请把下面这段接着写完：\n\n")
    assert trailing["content"] == "请把下面这段接着写完：\n\n"
    assert created["description"] is None  # 空白描述 = 没填
    assert created["tags"] == ["中文", "术语"]


async def test_create_defaults_category(client):
    assert (await _create(client, auth_headers(), title=_name("无分类")))["category"] == "通用"


async def test_blank_required_text_is_rejected(client):
    h = auth_headers()
    for body in (
        {"title": "   ", "content": "正文"},
        {"title": _name("x"), "content": ""},
        {"title": _name("x"), "content": "  \n  "},
        {"title": _name("x"), "content": "正文", "category": "  "},
    ):
        res = await client.post("/api/prompts", json=body, headers=h)
        assert res.status_code == 422, body


async def test_payload_limits(client):
    """超长内容必须在写进数据库之前被挡掉，而不是靠列宽炸成 500。"""
    h = auth_headers()
    cases: dict[str, object] = {
        "title": "标" * (TITLE_MAX + 1),
        "content": "正" * (CONTENT_MAX + 1),
        "description": "说" * (DESCRIPTION_MAX + 1),
        "tags": ["标签"] * (TAG_MAX_COUNT + 1),
    }
    for field, value in cases.items():
        res = await client.post(
            "/api/prompts",
            json={"title": _name("边界"), "content": "正文", field: value},
            headers=h,
        )
        assert res.status_code == 422, field

    # 恰好到上限要能存：上限是「不超过」而不是「小于」。
    ok = await client.post(
        "/api/prompts",
        json={
            "title": "标" * TITLE_MAX,
            "content": "正" * CONTENT_MAX,
            "tags": [f"标签{i}" for i in range(TAG_MAX_COUNT)],
        },
        headers=h,
    )
    assert ok.status_code == 201, ok.text

    long_tag = await client.post(
        "/api/prompts",
        json={
            "title": _name("长标签"),
            "content": "正文",
            "tags": ["标" * (TAG_MAX_LEN + 1)],
        },
        headers=h,
    )
    assert long_tag.status_code == 422


async def test_unknown_fields_are_ignored(client):
    res = await client.post(
        "/api/prompts",
        json={"title": _name("多余"), "content": "正文", "is_admin": True},
        headers=auth_headers(),
    )
    assert res.status_code == 201, res.text
    assert "is_admin" not in res.json()


# --------------------------------------------------------------------------- #
# PATCH 语义
# --------------------------------------------------------------------------- #


async def test_patch_only_touches_fields_it_sent(client):
    h = auth_headers()
    original = await _create(
        client,
        h,
        title=_name("原名"),
        content="原正文",
        category="编程",
        tags=["a", "b"],
        description="原描述",
    )
    res = await client.patch(
        f"/api/prompts/{original['id']}", json={"title": _name("新名")}, headers=h
    )
    assert res.status_code == 200, res.text
    after = res.json()
    assert after["title"] != original["title"]
    # 没提的字段一个都不许动 —— 这是 model_fields_set 存在的唯一理由。
    for field in ("content", "category", "tags", "description"):
        assert after[field] == original[field], field


async def test_patch_with_nothing_to_change_is_400(client):
    h = auth_headers()
    p = await _create(client, h, title=_name("空补丁"))
    for body in ({}, {"user_id": None}, {"sort_order": 3}):
        res = await client.patch(f"/api/prompts/{p['id']}", json=body, headers=h)
        assert res.status_code == 400, body
        assert res.json()["message"] == "没有需要更新的字段"


async def test_patch_clears_nullable_fields_only_when_told(client):
    h = auth_headers()
    p = await _create(client, h, title=_name("可空"), description="有描述", tags=["x"])
    res = await client.patch(
        f"/api/prompts/{p['id']}",
        json={"description": None, "tags": None},
        headers=h,
    )
    assert res.status_code == 200, res.text
    # description 是真正的空列；tags 列也可空，但对外承诺是数组，所以 null 归一成 []。
    assert res.json()["description"] is None
    assert res.json()["tags"] == []


async def test_patch_rejects_null_for_not_null_columns(client):
    h = auth_headers()
    p = await _create(client, h, title=_name("不可空"), content="正文")
    for field in ("title", "content", "category"):
        res = await client.patch(f"/api/prompts/{p['id']}", json={field: None}, headers=h)
        assert res.status_code == 422, field
        assert field in res.json()["message"]
    # 被拒的 PATCH 一个字节都没落地。
    unchanged = (await client.get(f"/api/prompts/{p['id']}", headers=h)).json()
    assert unchanged["title"] == p["title"]
    assert unchanged["category"] == p["category"]


async def test_patch_validates_the_new_value(client):
    h = auth_headers()
    p = await _create(client, h, title=_name("改坏"), content="正文")
    assert (
        await client.patch(f"/api/prompts/{p['id']}", json={"title": "  "}, headers=h)
    ).status_code == 422
    assert (
        await client.patch(
            f"/api/prompts/{p['id']}",
            json={"content": "正" * (CONTENT_MAX + 1)},
            headers=h,
        )
    ).status_code == 422


# --------------------------------------------------------------------------- #
# 归属隔离
# --------------------------------------------------------------------------- #


async def test_foreign_prompt_is_404_on_every_verb(client):
    h1 = auth_headers()
    mine = await _create(client, h1, title=_name("私人"))
    h2 = await _foreign_headers(client, uuid.uuid4().hex[:8])

    pid = mine["id"]
    assert (await client.get(f"/api/prompts/{pid}", headers=h2)).status_code == 404
    assert (
        await client.patch(f"/api/prompts/{pid}", json={"title": "改掉"}, headers=h2)
    ).status_code == 404
    assert (await client.delete(f"/api/prompts/{pid}", headers=h2)).status_code == 404
    # 404 不是「假装失败」：数据必须还在，主人照样读得到。
    still = (await client.get(f"/api/prompts/{pid}", headers=h1)).json()
    assert still["title"] == mine["title"]


async def test_list_and_search_never_leak_other_users_rows(client):
    h1 = auth_headers()
    secret_title = _name("绝密")
    await _create(client, h1, title=secret_title, content="只有主人看得见的正文")

    h2 = await _foreign_headers(client, uuid.uuid4().hex[:8])
    assert secret_title not in {t["title"] for t in await _list(client, h2)}
    # 搜索也不能变成旁路：拿标题当关键词同样搜不到。
    assert secret_title not in {t["title"] for t in await _list(client, h2, q=secret_title)}
    # 在主人眼里它在。
    assert secret_title in {t["title"] for t in await _list(client, h1, scope="mine")}


async def test_scope_param_controls_the_view(client, preset_template):
    h = auth_headers()
    mine = await _create(client, h, title=_name("我的"), category="编程")
    preset_id = str(preset_template.id)

    all_rows = _ids(await _list(client, h, scope="all", limit=500))
    mine_rows = _ids(await _list(client, h, scope="mine", limit=500))
    preset_rows = await _list(client, h, scope="preset", limit=500)

    assert {mine["id"], preset_id} <= all_rows
    assert mine["id"] in mine_rows and preset_id not in mine_rows
    assert preset_id in _ids(preset_rows) and mine["id"] not in _ids(preset_rows)
    # 预置视图里只有归属为空的行：用户模板一张都不该进来。
    assert all(row["user_id"] is None for row in preset_rows)


async def test_non_admin_never_sees_foreign_rows_even_with_scope_all(client):
    h1 = auth_headers()
    mine = await _create(client, h1, title=_name("不该漏"))
    h2 = await _foreign_headers(client, uuid.uuid4().hex[:8])
    # 显式要求 all 也只会给到「我的 + 预置」。
    visible = await _list(client, h2, scope="all", limit=500)
    assert mine["id"] not in _ids(visible)
    assert all(row["user_id"] != str(_SEEDED_USER) for row in visible)


async def test_admin_scope_all_sees_private_rows_but_mine_does_not(client, admin_token):
    mine = await _create(client, auth_headers(), title=_name("管理员可见"))
    ah = auth_headers(admin_token)
    assert mine["id"] in _ids(await _list(client, ah, scope="all", limit=500))
    assert mine["id"] not in _ids(await _list(client, ah, scope="mine", limit=500))


# --------------------------------------------------------------------------- #
# 预置模板：人人可读，只有管理员可写
# --------------------------------------------------------------------------- #


async def test_preset_is_readable_but_not_writable(client, preset_template):
    h = auth_headers()
    pid = str(preset_template.id)

    detail = (await client.get(f"/api/prompts/{pid}", headers=h)).json()
    assert detail["user_id"] is None
    assert detail["sort_order"] == 5  # 迁移里的展示顺序原样透出

    patch = await client.patch(f"/api/prompts/{pid}", json={"title": "改掉预置"}, headers=h)
    assert patch.status_code == 403
    assert "管理员" in patch.json()["message"]
    assert (
        await client.delete(f"/api/prompts/{pid}", headers=h)
    ).status_code == 403
    # 被拒之后原样还在。
    assert (
        await client.get(f"/api/prompts/{pid}", headers=h)
    ).json()["title"] == preset_template.title


async def test_admin_can_edit_a_preset(client, admin_token, preset_template):
    res = await client.patch(
        f"/api/prompts/{preset_template.id}",
        json={"category": "办公"},
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 200, res.text
    assert res.json()["category"] == "办公"


async def test_preset_title_uniqueness_does_not_restrain_users(
    client, db_session
):
    """唯一索引只覆盖 ``user_id IS NULL``：用户可以存两个同名模板。"""
    h = auth_headers()
    dup = _name("重名")
    first, second = (await _create(client, h, title=dup)), (await _create(client, h, title=dup))
    assert first["id"] != second["id"]

    preset = PromptTemplate(user_id=None, title="唯一预置标题", content="预置")
    db_session.add(preset)
    await db_session.commit()
    db_session.add(PromptTemplate(user_id=None, title="唯一预置标题", content="预置"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
    await db_session.delete(preset)
    await db_session.commit()


# --------------------------------------------------------------------------- #
# 搜索 / 分类 / 分页
# --------------------------------------------------------------------------- #


async def test_search_covers_title_description_and_content(client):
    h = auth_headers()
    by_title = await _create(client, h, title="费曼学习法", content="正文")
    by_desc = await _create(
        client, h, title=_name("标题"), description="关于熵的说明", content="正文"
    )
    by_body = await _create(
        client, h, title=_name("标题"), content="这里提到了柯尔莫哥洛夫"
    )

    assert by_title["id"] in _ids(await _list(client, h, q="费曼"))
    assert by_desc["id"] in _ids(await _list(client, h, q="熵"))
    assert by_body["id"] in _ids(await _list(client, h, q="柯尔莫哥洛夫"))
    assert await _list(client, h, q="查无此词" + uuid.uuid4().hex) == []


async def test_search_treats_like_wildcards_as_literal_text(client):
    """``%`` / ``_`` 是 LIKE 的元字符：不转义的话「100%」会命中一切。"""
    h = auth_headers()
    percent = await _create(client, h, title="折扣 100% 力度", content="正文")
    letter = await _create(client, h, title="折扣 100x 力度", content="正文")
    underscore = await _create(client, h, title="变量 a_b 的用法", content="正文")
    loose = await _create(client, h, title="变量 axb 的用法", content="正文")

    percent_hits = _ids(await _list(client, h, q="100%"))
    assert percent_hits == {percent["id"]}, "「100%」不得命中 100x 那行"
    assert letter["id"] not in percent_hits
    assert _ids(await _list(client, h, q="a_b")) == {underscore["id"]}
    # 转义没有把普通搜索一起弄坏：字面量照样搜得到。
    assert loose["id"] in _ids(await _list(client, h, q="axb"))


async def test_category_filter_is_exact(client):
    h = auth_headers()
    target = _name("分类")
    hit = await _create(client, h, title=_name("归入"), category=target, content="正文")
    await _create(
        client, h, title=_name("不归入"), category=f"{target}扩展", content="正文"
    )

    rows = await _list(client, h, category=target)
    assert _ids(rows) == {hit["id"]}
    assert all(row["category"] == target for row in rows)


async def test_pagination_slices_a_stable_order_without_overlap(client):
    h = auth_headers()
    created = _ids(
        [
            await _create(client, h, title=_name(f"分页{i}"), content="正文")
            for i in range(3)
        ]
    )
    full = [t["id"] for t in await _list(client, h, scope="mine", limit=500) if t["id"] in created]
    assert len(full) == 3, "同一行不得重复出现（排序必须有全序兜底）"

    page1 = [t["id"] for t in await _list(client, h, scope="mine", limit=2, offset=0)]
    page2 = [t["id"] for t in await _list(client, h, scope="mine", limit=2, offset=2)]
    assert page1 == full[:2] and page2 == full[2:]
    assert not set(page1) & set(page2)


async def test_pagination_bounds_are_validated(client):
    h = auth_headers()
    for params in ({"limit": 0}, {"limit": 5000}, {"offset": -1}):
        res = await client.get("/api/prompts", params=params, headers=h)
        assert res.status_code == 422, params


async def test_presets_sort_ahead_of_personal_templates(client, preset_template):
    h = auth_headers()
    mine = await _create(client, h, title=_name("排后面"), content="正文")
    order = [t["id"] for t in await _list(client, h, limit=500)]
    assert order.index(str(preset_template.id)) < order.index(mine["id"])


async def test_categories_endpoint_aggregates_visible_rows(client, preset_template):
    h = auth_headers()
    unique = _name("分类集")
    await _create(client, h, title=_name("归类"), category=unique, content="正文")

    res = await client.get("/api/prompts/categories", headers=h)
    assert res.status_code == 200, res.text
    assert preset_template.category == "写作"  # 下面断言的「写作」来自这一行预置
    assert unique in res.json() and "写作" in res.json()  # 写作来自预置行

    mine_only = (
        await client.get("/api/prompts/categories", params={"scope": "mine"}, headers=h)
    ).json()
    # 「写作」只来自预置行：scope=mine 的聚合里不能出现别人的分类。
    assert unique in mine_only and "写作" not in mine_only


# --------------------------------------------------------------------------- #
# 鉴权与路径
# --------------------------------------------------------------------------- #


async def test_endpoints_require_authentication(client):
    body = {"title": "x", "content": "y"}
    for method, path in (
        ("get", "/api/prompts"),
        ("post", "/api/prompts"),
        ("get", "/api/prompts/categories"),
        ("get", f"/api/prompts/{uuid.uuid4()}"),
        ("patch", f"/api/prompts/{uuid.uuid4()}"),
        ("delete", f"/api/prompts/{uuid.uuid4()}"),
    ):
        res = await client.request(method, path, json=body)
        assert res.status_code == 401, (method, path)


async def test_malformed_id_is_422_not_500(client):
    assert (
        await client.get("/api/prompts/not-a-uuid", headers=auth_headers())
    ).status_code == 422


async def test_missing_prompt_is_404(client):
    res = await client.get(f"/api/prompts/{uuid.uuid4()}", headers=auth_headers())
    assert res.status_code == 404
    assert res.json()["code"] == "not_found"
