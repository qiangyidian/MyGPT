"""消息版本历史（条目 31）：覆盖/删除之前必须留下能切回来的快照。

三件事是这条的目的，各自都有回归测试：

* ``regenerate`` 真的会 ``DELETE`` 上一条回答——所以版本不能挂 ``messages`` 外键，
  否则存档与删档发生在同一刻。
* 切回版本换的是**当前这一行**的内容，不新建消息：消息 id 是引用面板、点赞点踩、
  流式渲染的锚点。
* 版本总量必须封顶，且孤儿版本（所属消息已没了）也得在裁剪范围内。
"""
from __future__ import annotations

import uuid

from app.models import Message, MessageVersion
from app.services.chat_service import _delete_last_assistant_message
from app.services.message_versions import (
    MAX_VERSIONS_PER_MESSAGE,
    activate_version,
    latest_versions,
    prune_versions,
    snapshot_message,
)
from tests.conftest import auth_headers


async def _make_conversation(client, headers) -> uuid.UUID:
    resp = await client.post(
        "/api/conversations", json={"title": "版本测试"}, headers=headers
    )
    assert resp.status_code == 201
    return uuid.UUID(resp.json()["id"])


def _message(conv_id: uuid.UUID, **kwargs) -> Message:
    return Message(conversation_id=conv_id, **kwargs)


async def test_regenerate_snapshot_survives_the_deleted_message(client, db_session):
    h = auth_headers()
    conv_id = await _make_conversation(client, h)
    db_session.add(_message(conv_id, role="user", content="问题"))
    db_session.add(
        _message(
            conv_id,
            role="assistant",
            content="旧答案",
            model_name="gpt-x",
            total_tokens=42,
            metadata_={"citations": [{"document_name": "运维手册"}], "status": "done"},
        )
    )
    await db_session.commit()

    prompt = await _delete_last_assistant_message(db_session, conv_id)
    await db_session.commit()
    assert prompt == "问题"

    # 消息行没了，版本还在——这正是 message_versions 不建 messages 外键的原因。
    assert (
        await db_session.execute(
            Message.__table__.select().where(Message.__table__.c.role == "assistant")
        )
    ).rowcount == 0
    rows = list(await latest_versions(db_session, conv_id))
    assert [r.origin for r in rows] == ["regenerate"]
    assert rows[0].content == "旧答案"
    assert rows[0].model_name == "gpt-x"
    assert rows[0].total_tokens == 42
    # 引用来源跟着走：只存正文的话，切回旧版会把新版的答案配旧版的来源。
    assert rows[0].metadata_["citations"][0]["document_name"] == "运维手册"


async def test_activate_swaps_content_and_archives_the_displaced_version(
    client, db_session
):
    h = auth_headers()
    conv_id = await _make_conversation(client, h)
    msg = _message(conv_id, role="assistant", content="第一版")
    db_session.add(msg)
    await db_session.commit()

    old = await snapshot_message(db_session, msg, origin="regenerate")
    msg.content = "第二版"
    await db_session.commit()

    outcome = await activate_version(db_session, msg, old)
    await db_session.commit()
    assert outcome["changed"] is True
    await db_session.refresh(msg)
    assert msg.content == "第一版"

    origins = [r.origin for r in await latest_versions(db_session, conv_id)]
    # 被切回顶掉的那一版也必须留得下来，否则「反悔」没有路。
    assert "restore" in origins


async def test_activating_the_current_content_again_is_a_no_op(client, db_session):
    h = auth_headers()
    conv_id = await _make_conversation(client, h)
    msg = _message(conv_id, role="assistant", content="同一版")
    db_session.add(msg)
    await db_session.commit()
    old = await snapshot_message(db_session, msg, origin="regenerate")
    await db_session.commit()

    before = len(await latest_versions(db_session, conv_id))
    outcome = await activate_version(db_session, msg, old)
    await db_session.commit()
    assert outcome["changed"] is False
    # 点八次按钮不该攒出八条一模一样的历史。
    assert len(await latest_versions(db_session, conv_id)) == before


async def test_per_message_cap_prunes_the_oldest(client, db_session):
    h = auth_headers()
    conv_id = await _make_conversation(client, h)
    msg = _message(conv_id, role="assistant", content="v0")
    db_session.add(msg)
    await db_session.commit()

    total = MAX_VERSIONS_PER_MESSAGE + 5
    for i in range(total):
        msg.content = f"内容 {i}"
        await snapshot_message(db_session, msg, origin="regenerate")
    await db_session.commit()

    rows = list(await latest_versions(db_session, conv_id))
    assert len(rows) == MAX_VERSIONS_PER_MESSAGE
    # 裁的是最旧的，最近一版一定还在。
    assert rows[0].content == f"内容 {total - 1}"
    assert f"内容 {total - 1}" in {r.content for r in rows}


async def test_prune_reports_how_many_it_removed(client, db_session):
    h = auth_headers()
    conv_id = await _make_conversation(client, h)
    msg = _message(conv_id, role="assistant", content="x")
    db_session.add(msg)
    await db_session.commit()
    for i in range(3):
        msg.content = f"内容 {i}"
        await snapshot_message(db_session, msg, origin="edit")
    await db_session.commit()
    assert await prune_versions(db_session, uuid.uuid4()) == 0
    await db_session.commit()


async def test_versions_endpoint_is_owner_scoped_and_reads_back(client, db_session):
    h = auth_headers()
    conv_id = await _make_conversation(client, h)
    msg = _message(conv_id, role="assistant", content="第一版", model_name="m")
    db_session.add(msg)
    await db_session.commit()
    await snapshot_message(db_session, msg, origin="regenerate")
    await db_session.commit()

    listed = await client.get(f"/api/conversations/{conv_id}/versions", headers=h)
    assert listed.status_code == 200
    body = listed.json()
    assert len(body) == 1
    assert body[0]["origin"] == "regenerate"
    assert body[0]["content"] == "第一版"
    # 对外的键是 metadata（ORM 上叫 metadata_）。
    assert "metadata" in body[0] and "metadata_" not in body[0]

    stranger = await client.post(
        "/api/auth/register",
        json={
            "email": "version-snoop@example.com",
            "username": "versionsnoop",
            "password": "Passw0rd!",
        },
    )
    other = {"Authorization": f"Bearer {stranger.json()['access_token']}"}
    foreign = await client.get(f"/api/conversations/{conv_id}/versions", headers=other)
    assert foreign.status_code == 404


async def test_activate_endpoint_restores_content(client, db_session, monkeypatch):
    h = auth_headers()
    conv_id = await _make_conversation(client, h)
    msg = _message(conv_id, role="assistant", content="第一版")
    db_session.add(msg)
    await db_session.commit()
    old = await snapshot_message(db_session, msg, origin="regenerate")
    msg.content = "第二版"
    await db_session.commit()

    resp = await client.post(
        f"/api/conversations/{conv_id}/messages/{msg.id}/versions/{old.id}/activate",
        headers=h,
    )
    assert resp.status_code == 200
    assert resp.json()["changed"] is True

    detail = await client.get(f"/api/conversations/{conv_id}", headers=h)
    contents = [m["content"] for m in detail.json()["messages"]]
    assert contents == ["第一版"]

    missing = await client.post(
        f"/api/conversations/{conv_id}/messages/{msg.id}/versions/{uuid.uuid4()}/activate",
        headers=h,
    )
    assert missing.status_code == 404
