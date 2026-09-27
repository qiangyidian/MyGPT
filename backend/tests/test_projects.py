"""Projects: rename / delete / impact, ownership, and the soft-reference cascade.

``Conversation.project_id`` has NO foreign key, so a project delete cannot rely on
the database to un-file anything: the router has to do it. These tests pin that
behavior (plus the 400s that keep a bad PATCH from becoming a 500) and the
404-on-foreign-id rule the whole repo follows so existence is never leaked.

The session shares one in-memory DB across suites, so everything here is keyed off
rows the test itself creates (random tokens in names/emails).
"""
from __future__ import annotations

import uuid

from app.models import Conversation, Message
from tests.conftest import auth_headers

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")
NAME_MAX = 255


async def _project(client, headers, **body) -> dict:
    res = await client.post("/api/projects", json=body, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()


async def _conversation(client, headers, title: str) -> str:
    res = await client.post("/api/conversations", json={"title": title}, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _foreign_headers(client, token: str) -> dict[str, str]:
    reg = await client.post(
        "/api/auth/register",
        json={
            "email": f"proj-{token}@example.com",
            "username": f"proj-{token}",
            "password": "Passw0rd!",
        },
    )
    assert reg.status_code in (200, 201), reg.text
    return {"Authorization": f"Bearer {reg.json()['access_token']}"}


# --------------------------------------------------------------------------- #
# Rename
# --------------------------------------------------------------------------- #


async def test_rename_project(client):
    h = auth_headers()
    p = await _project(client, h, name=f"季度报告 {uuid.uuid4().hex[:8]}")
    new_name = f"年度报告 {uuid.uuid4().hex[:8]}"
    res = await client.patch(f"/api/projects/{p['id']}", json={"name": new_name}, headers=h)
    assert res.status_code == 200, res.text
    assert res.json()["name"] == new_name
    listed = (await client.get("/api/projects", headers=h)).json()
    assert [x["name"] for x in listed if x["id"] == p["id"]] == [new_name]


async def test_rename_trims_surrounding_whitespace(client):
    h = auth_headers()
    p = await _project(client, h, name="before")
    res = await client.patch(
        f"/api/projects/{p['id']}", json={"name": f"  after {uuid.uuid4().hex[:6]}  "}, headers=h
    )
    assert res.status_code == 200
    assert res.json()["name"] == res.json()["name"].strip()


async def test_rename_rejects_blank_without_deleting_the_row(client):
    h = auth_headers()
    p = await _project(client, h, name="keep me")
    for bad in ("", "   ", None):
        res = await client.patch(f"/api/projects/{p['id']}", json={"name": bad}, headers=h)
        assert res.status_code == 400, bad
        assert res.json()["message"] == "项目名称不能为空"
    # A rejected rename leaves the project exactly as it was.
    listed = (await client.get("/api/projects", headers=h)).json()
    assert [x["name"] for x in listed if x["id"] == p["id"]] == ["keep me"]


async def test_rename_rejects_overlong_name_as_400_not_500(client):
    h = auth_headers()
    p = await _project(client, h, name="short")
    res = await client.patch(
        f"/api/projects/{p['id']}", json={"name": "x" * (NAME_MAX + 1)}, headers=h
    )
    assert res.status_code == 400
    assert str(NAME_MAX) in res.json()["message"]


async def test_create_rejects_overlong_name_as_400(client):
    h = auth_headers()
    res = await client.post("/api/projects", json={"name": "x" * (NAME_MAX + 1)}, headers=h)
    assert res.status_code == 400


async def test_patch_is_partial_and_rejects_an_empty_body(client):
    h = auth_headers()
    p = await _project(
        client, h, name="原始名", description="重要说明", color="#00ff00"
    )
    res = await client.patch(
        f"/api/projects/{p['id']}",
        json={"name": "改名不动其他字段"},
        headers=h,
    )
    assert res.status_code == 200
    body = res.json()
    # Fields the caller never sent must survive untouched (exclude_unset PATCH).
    assert body["description"] == "重要说明"
    assert body["color"] == "#00ff00"

    empty = await client.patch(f"/api/projects/{p['id']}", json={}, headers=h)
    assert empty.status_code == 400
    assert empty.json()["message"] == "没有需要更新的字段"


async def test_patch_color_null_restores_default_and_bad_color_is_400(client):
    h = auth_headers()
    p = await _project(client, h, name="colors", color="#123456")
    res = await client.patch(f"/api/projects/{p['id']}", json={"color": None}, headers=h)
    assert res.status_code == 200
    assert res.json()["color"] == "#6366f1"
    for bad in ("red", "#12", "123456"):
        res = await client.patch(f"/api/projects/{p['id']}", json={"color": bad}, headers=h)
        assert res.status_code == 400, bad
        assert res.json()["message"] == "项目颜色需为 #RRGGBB 或 #RGB 格式"


# --------------------------------------------------------------------------- #
# Impact report
# --------------------------------------------------------------------------- #


async def test_impact_counts_conversations_and_messages(client, db_session):
    h = auth_headers()
    token = uuid.uuid4().hex[:8]
    p = await _project(client, h, name=f"impact {token}")
    first = await _conversation(client, h, f"one {token}")
    second = await _conversation(client, h, f"two {token}")
    for cid in (first, second):
        await client.post(f"/api/projects/{p['id']}/conversations/{cid}", headers=h)
    # Three messages on the first, one on the second.
    db_session.add_all(
        [
            Message(conversation_id=uuid.UUID(first), role="user", content="a"),
            Message(conversation_id=uuid.UUID(first), role="assistant", content="b"),
            Message(conversation_id=uuid.UUID(first), role="user", content="c"),
            Message(conversation_id=uuid.UUID(second), role="user", content="d"),
        ]
    )
    await db_session.commit()

    res = await client.get(f"/api/projects/{p['id']}/impact", headers=h)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["conversation_count"] == 2
    assert body["message_count"] == 4
    assert body["archived_conversation_count"] == 0
    # Nothing else is carried away by a project delete.
    assert body["deletes_conversations"] is False
    assert body["knowledge_base_count"] == 0


async def test_impact_sees_archived_conversations_of_the_project(client, db_session):
    h = auth_headers()
    p = await _project(client, h, name=f"archived {uuid.uuid4().hex[:8]}")
    cid = await _conversation(client, h, "to archive")
    await client.post(f"/api/projects/{p['id']}/conversations/{cid}", headers=h)
    await client.patch(f"/api/conversations/{cid}", json={"archived": True}, headers=h)
    body = (await client.get(f"/api/projects/{p['id']}/impact", headers=h)).json()
    assert body["conversation_count"] == 1
    assert body["archived_conversation_count"] == 1


async def test_impact_only_counts_the_callers_own_conversations(client, db_session):
    h = auth_headers()
    p = await _project(client, h, name=f"scoped {uuid.uuid4().hex[:8]}")
    mine = await _conversation(client, h, "mine")
    await client.post(f"/api/projects/{p['id']}/conversations/{mine}", headers=h)
    # A row that *claims* the project id but belongs to someone else: the impact
    # query is scoped by user_id, so it never joins another tenant's counts.
    other = Conversation(
        user_id=uuid.uuid4(), title="not mine", project_id=uuid.UUID(p["id"])
    )
    db_session.add(other)
    await db_session.commit()
    db_session.add(Message(conversation_id=other.id, role="user", content="x"))
    await db_session.commit()

    body = (await client.get(f"/api/projects/{p['id']}/impact", headers=h)).json()
    assert body["conversation_count"] == 1
    assert body["message_count"] == 0


# --------------------------------------------------------------------------- #
# Delete + the soft-reference cascade
# --------------------------------------------------------------------------- #


async def test_delete_unfiles_conversations_instead_of_deleting_them(client):
    h = auth_headers()
    p = await _project(client, h, name=f"doomed {uuid.uuid4().hex[:8]}")
    keep_a = await _conversation(client, h, "survivor a")
    keep_b = await _conversation(client, h, "survivor b")
    for cid in (keep_a, keep_b):
        await client.post(f"/api/projects/{p['id']}/conversations/{cid}", headers=h)
        detail = await client.get(f"/api/conversations/{cid}", headers=h)
        assert detail.json()["project_id"] == p["id"]

    res = await client.delete(f"/api/projects/{p['id']}", headers=h)
    assert res.status_code == 204

    # The conversations live on, and no longer point at a vanished project —
    # a dangling id made them invisible in the sidebar (neither filed nor
    # unfiled), which is the bug this cascade exists to prevent.
    for cid in (keep_a, keep_b):
        detail = await client.get(f"/api/conversations/{cid}", headers=h)
        assert detail.json()["project_id"] is None
    remaining = await client.get("/api/projects", headers=h)
    assert p["id"] not in [x["id"] for x in remaining.json()]


async def test_delete_leaves_other_projects_assignments_alone(client):
    h = auth_headers()
    suffix = uuid.uuid4().hex[:8]
    doomed = await _project(client, h, name=f"doomed {suffix}")
    survivor = await _project(client, h, name=f"survivor {suffix}")
    in_doomed = await _conversation(client, h, f"a {suffix}")
    in_survivor = await _conversation(client, h, f"b {suffix}")
    await client.post(f"/api/projects/{doomed['id']}/conversations/{in_doomed}", headers=h)
    await client.post(
        f"/api/projects/{survivor['id']}/conversations/{in_survivor}", headers=h
    )

    assert (await client.delete(f"/api/projects/{doomed['id']}", headers=h)).status_code == 204
    kept = await client.get(f"/api/conversations/{in_survivor}", headers=h)
    assert kept.json()["project_id"] == survivor["id"]


async def test_every_project_route_is_404_on_foreign_id(client):
    h = auth_headers()
    p = await _project(client, h, name=f"private {uuid.uuid4().hex[:8]}")
    cid = await _conversation(client, h, "private conv")
    await client.post(f"/api/projects/{p['id']}/conversations/{cid}", headers=h)
    other = await _foreign_headers(client, uuid.uuid4().hex[:8])

    # 404, never 403: a 403 would confirm the project exists.
    checks = [
        await client.patch(f"/api/projects/{p['id']}", json={"name": "hijack"}, headers=other),
        await client.get(f"/api/projects/{p['id']}/impact", headers=other),
        await client.delete(f"/api/projects/{p['id']}", headers=other),
        await client.post(f"/api/projects/{p['id']}/conversations/{cid}", headers=other),
    ]
    for res in checks:
        assert res.status_code == 404, res.request.method
    assert (await client.get("/api/projects", headers=other)).json() == []
    # Nothing changed on the owner's side: same name, still one conversation.
    impact = await client.get(f"/api/projects/{p['id']}/impact", headers=h)
    assert impact.json()["conversation_count"] == 1
    detail = await client.get(f"/api/conversations/{cid}", headers=h)
    assert detail.json()["project_id"] == p["id"]
