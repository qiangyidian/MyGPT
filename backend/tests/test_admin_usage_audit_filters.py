"""``GET /api/admin/usage`` 报表与 ``GET /api/admin/audit`` 的筛选 + 分页。

每个用例只在自己那段 2020 年的私有区间里断言：整个测试会话共用一个内存库
（conftest 的 ``StaticPool``），别的套件也在往 ``messages`` / ``audit_events``
写行，按「一共几条」断言只会随执行顺序产生假阳性。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models import AuditEvent, Conversation, Message, User
from tests.conftest import auth_headers

# 每次 _slot() 取走一段互不重叠的日历区间（够放 60 个用例）。留两天空隙：
# 「区间外的那一行」落在下一天，不能被下一个用例当成自己的数据。
_CURSOR = [datetime(2020, 1, 2, 8, 0, tzinfo=UTC)]


def _slot(length_days: int = 3):
    start = _CURSOR[0]
    _CURSOR[0] = start + timedelta(days=length_days + 2)
    end = start + timedelta(days=length_days - 1)
    return start, start.date().isoformat(), end.date().isoformat()


async def _user(db_session, tag: str) -> User:
    user = User(
        email=f"{tag}{uuid.uuid4().hex[:8]}@example.com",
        username=f"{tag}{uuid.uuid4().hex[:8]}",
        password_hash="x",
        role="user",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def _conversation(db_session, user: User, stamp: datetime) -> Conversation:
    conv = Conversation(user_id=user.id, title="报表用例", created_at=stamp, updated_at=stamp)
    db_session.add(conv)
    await db_session.commit()
    return conv


async def _message(
    db_session,
    conv: Conversation,
    *,
    role: str,
    created_at: datetime,
    model_name: str | None = None,
    prompt: int | None = None,
    completion: int | None = None,
    total: int | None = None,
    cost: float | None = None,
) -> None:
    db_session.add(
        Message(
            conversation_id=conv.id,
            role=role,
            content="hi",
            model_name=model_name,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            cost_usd=cost,
            created_at=created_at,
        )
    )
    await db_session.commit()


async def _usage(client, admin_token, *, start: str, end: str, **params) -> dict:
    res = await client.get(
        "/api/admin/usage",
        params={"start": start, "end": end, **params},
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 200, res.text
    return res.json()


# --------------------------------------------------------------------------- #
# 用量报表
# --------------------------------------------------------------------------- #
async def test_usage_report_groups_by_day_with_tokens_and_cost(
    client, admin_token, db_session
):
    base, start, end = _slot()
    user = await _user(db_session, "day")
    conv = await _conversation(db_session, user, base)
    await _message(db_session, conv, role="user", created_at=base)
    await _message(
        db_session,
        conv,
        role="assistant",
        created_at=base + timedelta(minutes=1),
        model_name="m-a",
        prompt=10,
        completion=5,
        total=15,
        cost=0.25,
    )
    day_after = base + timedelta(days=1)
    await _message(
        db_session,
        conv,
        role="assistant",
        created_at=day_after,
        model_name="m-a",
        prompt=7,
        completion=3,
        total=10,
        cost=0.5,
    )
    # 区间外的一行：既不该进合计，也不该进分组。
    await _message(
        db_session,
        conv,
        role="assistant",
        created_at=base + timedelta(days=3),
        model_name="m-a",
        total=99,
        cost=9.0,
    )

    body = await _usage(client, admin_token, start=start, end=end)
    assert body["group_by"] == "day"
    assert body["start"] == start and body["end"] == end
    by_day = {row["key"]: row for row in body["items"]}
    assert set(by_day) == {base.date().isoformat(), day_after.date().isoformat()}

    first = by_day[base.date().isoformat()]
    assert first["messages"] == 2
    assert first["user_messages"] == 1
    # 请求数 = assistant 行数：token 与成本只记在这些行上。
    assert first["requests"] == 1
    assert first["prompt_tokens"] == 10
    assert first["completion_tokens"] == 5
    assert first["total_tokens"] == 15
    assert first["cost_usd"] == 0.25

    second = by_day[day_after.date().isoformat()]
    assert second["messages"] == 1
    assert second["user_messages"] == 0
    assert second["requests"] == 1
    assert second["total_tokens"] == 10

    totals = body["totals"]
    assert totals == {
        "messages": 3,
        "user_messages": 1,
        "requests": 2,
        "prompt_tokens": 17,
        "completion_tokens": 8,
        "total_tokens": 25,
        "cost_usd": 0.75,
    }
    assert body["total"] == 2


async def test_usage_report_day_boundary_includes_whole_end_day(
    client, admin_token, db_session
):
    base, start, end = _slot(length_days=2)
    user = await _user(db_session, "bound")
    conv = await _conversation(db_session, user, base)
    await _message(
        db_session,
        conv,
        role="assistant",
        created_at=datetime(
            base.year, base.month, base.day, 23, 59, 59, 500000, tzinfo=UTC
        ),
    )
    next_day = base + timedelta(days=1)
    await _message(
        db_session,
        conv,
        role="assistant",
        created_at=datetime(next_day.year, next_day.month, next_day.day, 0, 0, tzinfo=UTC),
    )

    body = await _usage(client, admin_token, start=start, end=start)
    assert [row["key"] for row in body["items"]] == [base.date().isoformat()]
    assert body["items"][0]["requests"] == 1

    body = await _usage(client, admin_token, start=start, end=end)
    assert {row["key"] for row in body["items"]} == {
        base.date().isoformat(),
        next_day.date().isoformat(),
    }
    assert body["totals"]["requests"] == 2


async def test_usage_report_model_group_falls_back_for_missing_model_name(
    client, admin_token, db_session
):
    base, start, end = _slot(length_days=1)
    user = await _user(db_session, "model")
    conv = await _conversation(db_session, user, base)
    await _message(db_session, conv, role="assistant", created_at=base, model_name=None, total=3)
    for name, total, cost in (("qwen-max", 4, 0.25), ("qwen-max", 8, 0.5)):
        await _message(
            db_session,
            conv,
            role="assistant",
            created_at=base,
            model_name=name,
            total=total,
            cost=cost,
        )

    body = await _usage(client, admin_token, start=start, end=end, group_by="model")
    rows = {row["label"]: row for row in body["items"]}
    assert set(rows) == {"qwen-max", "未记录模型"}
    assert rows["qwen-max"]["requests"] == 2
    assert rows["qwen-max"]["total_tokens"] == 12
    assert rows["qwen-max"]["cost_usd"] == 0.75
    # NULL 模型名要有一行兜底（key 是空串），而不是在报表里挂一行空白。
    assert rows["未记录模型"]["key"] == ""
    assert rows["未记录模型"]["requests"] == 1
    assert rows["未记录模型"]["cost_usd"] == 0.0
    # 请求数多的在前，同数按分组键 —— OFFSET 分页要全序。
    assert [row["label"] for row in body["items"]] == ["qwen-max", "未记录模型"]


async def test_usage_report_user_group_carries_identity(client, admin_token, db_session):
    base, start, end = _slot(length_days=1)
    user = await _user(db_session, "who")
    conv = await _conversation(db_session, user, base)
    await _message(
        db_session,
        conv,
        role="assistant",
        created_at=base,
        model_name="m",
        total=9,
        cost=0.4,
    )

    body = await _usage(client, admin_token, start=start, end=end, group_by="user")
    assert len(body["items"]) == 1
    row = body["items"][0]
    assert row["key"] == str(user.id)
    assert row["label"] == user.email
    assert row["email"] == user.email
    assert row["username"] == user.username
    assert row["total_tokens"] == 9
    assert row["cost_usd"] == 0.4


async def test_usage_report_pages_do_not_repeat_or_skip_groups(
    client, admin_token, db_session
):
    base, start, end = _slot(length_days=1)
    user = await _user(db_session, "page")
    conv = await _conversation(db_session, user, base)
    for index in range(6):
        await _message(
            db_session,
            conv,
            role="assistant",
            created_at=base,
            model_name=f"same-{index}",
        )

    full = await _usage(client, admin_token, start=start, end=end, group_by="model")
    assert full["total"] == 6
    collected: list[str] = []
    offset = 0
    while offset < full["total"]:
        page = await _usage(
            client, admin_token, start=start, end=end, group_by="model", limit=2, offset=offset
        )
        assert page["total"] == 6
        collected.extend(row["key"] for row in page["items"])
        offset += 2
    # 翻页拼起来 = 一次取全，且不重不漏。
    assert collected == [row["key"] for row in full["items"]]
    assert len(set(collected)) == 6


async def test_usage_report_rejects_unknown_group_and_bad_range(client, admin_token):
    res = await client.get(
        "/api/admin/usage",
        params={"start": "2020-11-01", "end": "2020-11-03", "group_by": "nope"},
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 400
    assert "未知分组维度" in res.json()["message"]

    res = await client.get(
        "/api/admin/usage",
        params={"start": "2020-11-03", "end": "2020-11-01"},
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 400
    assert res.json()["message"] == "开始日期不能晚于结束日期"

    res = await client.get(
        "/api/admin/usage",
        params={"start": "2020-01-01", "end": "2021-01-01"},
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 400
    assert "日期区间最多" in res.json()["message"]


async def test_usage_stats_still_returns_day_buckets_for_the_dashboard(db_session):
    """``/api/admin/stats`` 的 usage 仍是按天的 ``UsageStat``：报表没把它换掉。"""
    from app.services import admin_service

    stats = await admin_service.usage_stats(db_session)
    assert isinstance(stats, list)
    for row in stats:
        assert set(row.model_dump()) == {
            "date",
            "conversations",
            "messages",
            "user_messages",
            "assistant_messages",
            "tool_calls",
        }
        assert row.date


# --------------------------------------------------------------------------- #
# 审计列表
# --------------------------------------------------------------------------- #
async def _audit(db_session, actor: User | None, *, action: str, target: str, stamp: datetime):
    event = AuditEvent(
        actor_id=actor.id if actor else None,
        action=action,
        target=target,
        detail={"k": 1},
        created_at=stamp,
    )
    db_session.add(event)
    await db_session.commit()
    return event


async def _audit_list(client, admin_token, **params) -> dict:
    res = await client.get(
        "/api/admin/audit", params=params, headers=auth_headers(admin_token)
    )
    assert res.status_code == 200, res.text
    return res.json()


async def test_audit_filters_by_action_exact_and_prefix(client, admin_token, db_session):
    base, start, end = _slot(length_days=1)
    token = uuid.uuid4().hex[:6]
    actor = await _user(db_session, "act")
    exact = f"audit-{token}:one"
    prefixed = f"audit-{token}:sub"
    outside = f"audit-other-{token}"
    for action in (exact, prefixed, outside):
        await _audit(db_session, actor, action=action, target="t", stamp=base)

    body = await _audit_list(client, admin_token, action=exact)
    assert [row["action"] for row in body["items"]] == [exact]
    assert body["total"] == 1
    # 操作人要看得懂，不能只丢一个 UUID 给运营。
    assert body["items"][0]["actor_email"] == actor.email
    assert body["items"][0]["actor_username"] == actor.username

    body = await _audit_list(client, admin_token, action_prefix=f"audit-{token}:")
    assert {row["action"] for row in body["items"]} == {exact, prefixed}
    assert body["total"] == 2

    body = await _audit_list(
        client, admin_token, start=start, end=end, action_prefix=f"audit-{token}:"
    )
    assert body["total"] == 2


async def test_audit_keyword_escapes_like_wildcards(client, admin_token, db_session):
    base, _start, _end = _slot(length_days=1)
    token = uuid.uuid4().hex[:6]
    actor = await _user(db_session, "like")
    for letter, target in (
        ("a", f"{token}%x"),
        ("b", f"{token}ZZx"),
        ("c", f"{token}_y"),
        ("d", f"{token}Zy"),
    ):
        await _audit(
            db_session, actor, action=f"esc-{letter}-{token}", target=target, stamp=base
        )

    # 「%」「_」是用户真能打出来的字符：不转义时它们会把整张表当成命中。
    body = await _audit_list(client, admin_token, q=f"{token}%x")
    assert [row["target"] for row in body["items"]] == [f"{token}%x"]

    body = await _audit_list(client, admin_token, q=f"{token}_y")
    assert [row["target"] for row in body["items"]] == [f"{token}_y"]

    body = await _audit_list(client, admin_token, q=token)
    assert body["total"] == 4


async def test_audit_filters_actor_by_email_or_id(client, admin_token, db_session):
    base, _start, _end = _slot(length_days=1)
    token = uuid.uuid4().hex[:6]
    mine = await _user(db_session, "who")
    stranger = await _user(db_session, "other")
    action = f"actor-{token}"
    await _audit(db_session, mine, action=action, target="t1", stamp=base)
    await _audit(db_session, stranger, action=action, target="t2", stamp=base)
    await _audit(db_session, None, action=action, target="t3", stamp=base)

    by_email = await _audit_list(client, admin_token, action=action, actor=mine.email)
    assert [row["target"] for row in by_email["items"]] == ["t1"]
    assert by_email["total"] == 1

    by_id = await _audit_list(client, admin_token, action=action, actor=str(mine.id))
    assert [row["target"] for row in by_id["items"]] == ["t1"]

    by_username = await _audit_list(client, admin_token, action=action, actor=mine.username)
    assert [row["target"] for row in by_username["items"]] == ["t1"]

    # 没有 actor 过滤时系统事件（actor_id 为空）也要能看见。
    everything = await _audit_list(client, admin_token, action=action)
    assert everything["total"] == 3


async def test_audit_date_range_and_bad_range(client, admin_token, db_session):
    base, start, end = _slot(length_days=2)
    token = uuid.uuid4().hex[:6]
    actor = await _user(db_session, "range")
    action = f"ranged-{token}"
    edge = (base + timedelta(days=1)).replace(hour=23, minute=59, second=59)
    await _audit(db_session, actor, action=action, target="in", stamp=base)
    await _audit(db_session, actor, action=action, target="edge", stamp=edge)
    await _audit(
        db_session,
        actor,
        action=action,
        target="out",
        stamp=edge + timedelta(minutes=1),
    )

    body = await _audit_list(client, admin_token, action=action, start=start, end=end)
    assert [row["target"] for row in body["items"]] == ["edge", "in"]
    assert body["total"] == 2

    body = await _audit_list(client, admin_token, action=action, start=start, end=start)
    assert [row["target"] for row in body["items"]] == ["in"]

    res = await client.get(
        "/api/admin/audit",
        params={"start": end, "end": start},
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 400
    assert res.json()["message"] == "开始日期不能晚于结束日期"


async def test_audit_pages_are_disjoint_when_timestamps_tie(client, admin_token, db_session):
    """同一瞬间写入的多条事件也要能翻页：排序必须有稳定的次级键。"""
    base, _start, _end = _slot(length_days=1)
    token = uuid.uuid4().hex[:6]
    actor = await _user(db_session, "tie")
    action = f"tie-{token}"
    created = [
        await _audit(db_session, actor, action=action, target=f"t{i}", stamp=base)
        for i in range(5)
    ]
    expected = sorted(str(event.id) for event in created)

    full = await _audit_list(client, admin_token, action=action, limit=100)
    assert [row["id"] for row in full["items"]] == expected

    collected: list[str] = []
    offset = 0
    while offset < full["total"]:
        page = await _audit_list(
            client, admin_token, action=action, limit=2, offset=offset
        )
        collected.extend(row["id"] for row in page["items"])
        offset += 2
    assert collected == expected
    assert len(set(collected)) == 5


async def test_audit_requires_admin(client, auth_token):
    res = await client.get("/api/admin/audit", headers=auth_headers(auth_token))
    assert res.status_code == 403
