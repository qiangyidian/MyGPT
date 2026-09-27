"""Sidebar paging + server-side search (``GET /api/conversations``).

Hermetic by construction: the whole test session shares ONE in-memory database
(conftest's ``StaticPool``), so other suites' conversations are present. Every
assertion below is therefore scoped to rows this test created — a random token in
the title plus a ``q=`` filter — never to "the list has exactly N items".

Seeding goes through ``db_session`` with explicit ``updated_at`` values because the
list's ``ORDER BY updated_at`` needs a total order: SQLite's ``func.now()`` is
second-granular, so several rows created in one run can share a timestamp and page
boundaries would then overlap for reasons unrelated to the code under test.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, UTC

from app.models import Conversation, Message
from tests.conftest import auth_headers

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")
_BASE = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
_PAGE_SIZE = 2


async def _seed(
    db_session,
    titles: list[str],
    *,
    token: str,
    archived: bool = False,
    preview: str | None = None,
    start: int = 0,
) -> list[Conversation]:
    """Insert conversations at distinct ``start``-anchored minutes (oldest first)."""
    rows: list[Conversation] = []
    for i, title in enumerate(titles):
        stamp = _BASE + timedelta(minutes=start + i)
        conv = Conversation(
            user_id=_SEEDED_USER,
            title=f"{title} {token}",
            is_archived=archived,
            last_message_preview=preview,
            created_at=stamp,
            updated_at=stamp,
        )
        db_session.add(conv)
        rows.append(conv)
    await db_session.commit()
    return rows


async def _pin(db_session, conv: Conversation) -> None:
    await db_session.execute(
        Conversation.__table__.update()
        .where(Conversation.id == conv.id)
        .values(is_pinned=True)
    )
    await db_session.commit()


async def _page(client, headers, *, token: str, limit: int, offset: int) -> list[dict]:
    res = await client.get(
        "/api/conversations",
        params={"q": token, "limit": limit, "offset": offset},
        headers=headers,
    )
    assert res.status_code == 200
    return list(res.json())


async def test_pages_are_newest_first_and_cover_every_row(client, db_session):
    h = auth_headers()
    token = uuid.uuid4().hex[:10]
    titles = ["alpha", "beta", "gamma", "delta", "epsilon"]
    await _seed(db_session, titles, token=token)

    seen: list[str] = []
    titles_seen: list[str] = []
    offset = 0
    for _ in range(len(titles) + 2):  # a short page ends the walk
        page = await _page(client, h, token=token, limit=_PAGE_SIZE, offset=offset)
        assert len(page) <= _PAGE_SIZE
        seen += [c["id"] for c in page]
        titles_seen += [c["title"] for c in page]
        if len(page) < _PAGE_SIZE:
            break
        offset += len(page)

    # 5 rows at page size 2 -> 2 + 2 + 1, no duplicates and nothing skipped.
    assert len(seen) == 5
    assert len(set(seen)) == 5
    assert titles_seen == [f"{t} {token}" for t in reversed(titles)]


async def test_pinned_row_leads_page_one_and_paging_continues_after_it(
    client, db_session
):
    h = auth_headers()
    token = uuid.uuid4().hex[:10]
    # "stale" is the OLDEST row but pinned: the sidebar's pinned-first order must
    # not make page 2 re-serve it or skip the newest one.
    stale = (await _seed(db_session, ["stale"], token=token))[0]
    mid, newest = await _seed(db_session, ["mid", "newest"], token=token, start=5)
    await _pin(db_session, stale)

    page0 = await _page(client, h, token=token, limit=1, offset=0)
    page1 = await _page(client, h, token=token, limit=1, offset=1)
    page2 = await _page(client, h, token=token, limit=1, offset=2)
    assert [c["id"] for c in page0] == [str(stale.id)]
    assert [c["id"] for c in page1] == [str(newest.id)]
    assert [c["id"] for c in page2] == [str(mid.id)]


async def test_search_is_case_insensitive(client, db_session):
    h = auth_headers()
    token = uuid.uuid4().hex[:10]
    (row,) = await _seed(db_session, ["Quarterly Report"], token=token)
    for probe in ("quarterly report", "QUARTERLY", "QuArErLy RepORt"):
        res = await client.get(
            "/api/conversations", params={"q": f"{probe} {token}"}, headers=h
        )
        assert [c["id"] for c in res.json()] == [str(row.id)], probe


async def test_search_treats_like_wildcards_as_literal_text(client, db_session):
    h = auth_headers()
    token = uuid.uuid4().hex[:10]
    await _seed(db_session, ["report"], token=token)

    # Unescaped, both probes would match: ``%`` = anything, and the ``_`` in
    # "_<token>" would eat the space in "report <token>".
    for probe in (f"%{token}", f"_{token}"):
        res = await client.get("/api/conversations", params={"q": probe}, headers=h)
        assert res.json() == [], probe

    res = await client.get("/api/conversations", params={"q": token}, headers=h)
    assert [c["title"] for c in res.json()] == [f"report {token}"]


async def test_search_covers_last_message_preview(client, db_session):
    h = auth_headers()
    token = uuid.uuid4().hex[:10]
    await _seed(
        db_session, ["untitled"], token=token, preview=f"invoice-{token} paid in full"
    )
    res = await client.get(
        "/api/conversations", params={"q": f"invoice-{token}"}, headers=h
    )
    assert len(res.json()) == 1


async def test_message_content_is_out_of_scope_for_sidebar_search(client, db_session):
    """Documents the deliberate limit: no trigram/GIN index on messages.content.

    A leading-wildcard scan of that column would turn the sidebar's hot path into
    a full-history scan, so ``q`` matches the title and the preview only.
    """
    h = auth_headers()
    token = uuid.uuid4().hex[:10]
    (row,) = await _seed(db_session, ["nothing related"], token=token)
    db_session.add(
        Message(
            conversation_id=row.id,
            role="user",
            content=f"please find {token} inside the body",
        )
    )
    await db_session.commit()

    res = await client.get("/api/conversations", params={"q": token}, headers=h)
    assert [c["id"] for c in res.json()] == [str(row.id)]
    res = await client.get(
        "/api/conversations", params={"q": f"inside the body {token}"}, headers=h
    )
    assert res.json() == []


async def test_archived_view_pages_independently_of_active_view(client, db_session):
    h = auth_headers()
    token = uuid.uuid4().hex[:10]
    keep_a, keep_b, keep_c = await _seed(
        db_session, ["keep-a", "keep-b", "keep-c"], token=token
    )
    (gone,) = await _seed(db_session, ["gone"], token=token, archived=True)

    archived = await client.get(
        "/api/conversations",
        params={"q": token, "archived": True, "limit": 10, "offset": 0},
        headers=h,
    )
    assert [c["id"] for c in archived.json()] == [str(gone.id)]

    seen: list[str] = []
    for offset in (0, 1, 2, 3):
        page = await _page(client, h, token=token, limit=1, offset=offset)
        seen += [c["id"] for c in page]
    assert seen == [str(c.id) for c in (keep_c, keep_b, keep_a)]


async def test_out_of_range_offset_is_empty_and_inputs_are_clamped(client, db_session):
    h = auth_headers()
    token = uuid.uuid4().hex[:10]
    await _seed(db_session, ["solo"], token=token)

    res = await client.get(
        "/api/conversations", params={"q": token, "limit": 10, "offset": 99}, headers=h
    )
    assert res.status_code == 200
    assert res.json() == []

    # A negative OFFSET is an error on Postgres and a blank page on SQLite; the
    # service clamps it to 0 so the first page still comes back.
    res = await client.get(
        "/api/conversations",
        params={"q": token, "limit": 200, "offset": -5},
        headers=h,
    )
    assert res.status_code == 200
    assert len(res.json()) == 1


async def test_search_never_crosses_into_another_users_conversations(
    client, db_session
):
    token = uuid.uuid4().hex[:10]
    await _seed(db_session, ["mine"], token=token)
    reg = await client.post(
        "/api/auth/register",
        json={
            "email": f"pager-{token}@example.com",
            "username": f"pager-{token}",
            "password": "Passw0rd!",
        },
    )
    assert reg.status_code in (200, 201)
    other = {"Authorization": f"Bearer {reg.json()['access_token']}"}

    res = await client.get("/api/conversations", params={"q": token}, headers=other)
    assert res.json() == []
