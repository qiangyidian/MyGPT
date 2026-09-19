"""Guards for the KB / document listing hot paths (implicit chunk loads).

Regression class: ``Document.chunks`` and ``KnowledgeBase.documents`` used to be
``lazy="selectin"``, so *any* ``select(KnowledgeBase)`` / ``select(Document)``
eagerly pulled every chunk body (Text rows) of every document into Python — the
KB list, the document list, the admin's platform-wide listing, the keyword
retriever's join, and the KB ownership lookup of every RAG chat turn.

These tests pin down what makes the fix stick:

1. the list endpoints emit no statement that reads chunk *rows* (the two
   ``COUNT`` aggregates are expected, and are the only allowed ``document_chunks``
   contact);
2. the collections are ``lazy="raise"``, so a future caller that reaches for
   ``kb.documents`` / ``doc.chunks`` fails loudly in CI instead of quietly
   re-introducing the full read;
3. the delete paths still remove chunk + document rows. That used to happen via
   the ORM's delete-orphan cascade, which needs the collection loaded; with
   ``raise`` it has to be explicit SQL — and on SQLite (CI) the FK's
   ``ON DELETE CASCADE`` is not enforced either, so nothing else cleans up;
4. the new ``limit`` / ``offset`` page parameters really page.
"""
from __future__ import annotations

import contextlib
import uuid

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import InvalidRequestError

from app.models import Document, DocumentChunk, KnowledgeBase, User
from tests.conftest import TestSessionLocal, auth_headers, get_access_token

SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _captured_statements():
    """Collect every SQL statement the test engine executes inside the block."""
    from tests.conftest import test_engine

    seen: list[str] = []

    def _listen(dbapi_conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    sync_engine = test_engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _listen)
    try:
        yield seen
    finally:
        event.remove(sync_engine, "before_cursor_execute", _listen)


def _chunk_row_reads(statements: list[str]) -> list[str]:
    """Statements reading chunk rows. Counting them in aggregate is fine."""
    return [s for s in statements if "document_chunks" in s.lower() and "count(" not in s.lower()]


def _document_row_reads(statements: list[str]) -> list[str]:
    """Statements reading full document rows (``filename`` is documents-only)."""
    return [s for s in statements if "documents.filename" in s.lower()]


def _kb_shape_statements(statements: list[str]) -> list[str]:
    """Statements touching the knowledge_bases / documents / chunks tables."""
    out = []
    for s in statements:
        low = s.lower()
        if "knowledge_bases" in low or "documents" in low or "document_chunks" in low:
            out.append(s)
    return out


@pytest.fixture
def no_vectors(monkeypatch):
    """Stub the vector store: no Qdrant in tests, and no connection timeouts."""
    calls: list[tuple[str, dict]] = []

    class _Stub:
        async def delete_by_filter(self, collection, filters=None):
            calls.append((collection, dict(filters or {})))

    def _get():
        return _Stub()

    import app.rag.qdrant_store as _qdrant
    from app.services import document_service

    monkeypatch.setattr(_qdrant, "get_vector_store", _get)
    monkeypatch.setattr(document_service, "get_vector_store", _get)
    return calls


async def _seed_kb(
    db,
    *,
    user_id: uuid.UUID | None = None,
    documents: int = 1,
    chunks_per_document: int = 3,
    name: str = "kb",
) -> KnowledgeBase:
    """KB + documents + chunk rows, written straight to the DB (no uploads)."""
    kb = KnowledgeBase(user_id=user_id or SEEDED_USER, name=name)
    db.add(kb)
    await db.flush()
    for d in range(documents):
        doc = Document(
            knowledge_base_id=kb.id,
            filename=f"{name}-doc-{d}.txt",
            file_path=f"/tmp/{name}-doc-{d}.txt",
            file_type=".txt",
            status="indexed",
            chunk_count=chunks_per_document,
        )
        db.add(doc)
        await db.flush()
        db.add_all(
            [
                DocumentChunk(
                    document_id=doc.id,
                    knowledge_base_id=kb.id,
                    chunk_index=i,
                    content=f"{name} chunk body {d}-{i} " + ("x" * 200),
                    token_count=50,
                    metadata_={},
                )
                for i in range(chunks_per_document)
            ]
        )
    await db.commit()
    return kb


async def _docs_of(db, kb_id) -> list[Document]:
    return list(
        (
            await db.execute(
                select(Document)
                .where(Document.knowledge_base_id == kb_id)
                .order_by(Document.filename)
            )
        )
        .scalars()
        .all()
    )


async def _count_where(db, model, *criteria) -> int:
    stmt = select(func.count()).select_from(model)
    if criteria:
        stmt = stmt.where(*criteria)
    return (await db.execute(stmt)).scalar_one()


async def _fresh_owner(db, *, prefix: str = "kbown") -> tuple[User, dict]:
    """A dedicated user + auth headers.

    The in-memory SQLite database is created once per test SESSION (StaticPool)
    and never truncated, so every listing assertion has to be scoped to rows this
    test owns — otherwise another file's rows show up in "the whole platform".
    """
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"{prefix}-{suffix}@example.com",
        username=f"{prefix}-{suffix}",
        password_hash="not-a-real-hash",
        role="user",
        is_active=True,
    )
    db.add(user)
    await db.commit()
    return user, auth_headers(get_access_token(user.id))


# --------------------------------------------------------------------------- #
# 1 + 2: listings read no chunk rows, and cannot be made to
# --------------------------------------------------------------------------- #
async def test_kb_listing_does_not_read_chunk_rows(client, db_session):
    owner, hdrs = await _fresh_owner(db_session)
    await _seed_kb(db_session, user_id=owner.id, documents=4, chunks_per_document=25, name="big")
    await _seed_kb(db_session, user_id=owner.id, documents=1, chunks_per_document=5, name="small")

    with _captured_statements() as seen:
        res = await client.get("/api/knowledge-bases", headers=hdrs)
    assert res.status_code == 200, res.text

    assert _chunk_row_reads(seen) == [], (
        "the KB list may only COUNT chunks, never read their rows: "
        f"{_chunk_row_reads(seen)[:1]}"
    )
    assert _document_row_reads(seen) == [], "the KB list must not load document rows"
    # One page of KBs + two GROUP BY counts. Anything growing with the number of
    # documents is the N+1 / eager-load shape this guard exists for.
    assert len(_kb_shape_statements(seen)) == 3, _kb_shape_statements(seen)

    by_name = {k["name"]: k for k in res.json()}
    assert set(by_name) == {"big", "small"}
    assert by_name["big"]["document_count"] == 4
    assert by_name["big"]["chunk_count"] == 100
    assert by_name["small"]["document_count"] == 1
    assert by_name["small"]["chunk_count"] == 5


async def test_document_listing_does_not_read_chunk_rows(client, db_session):
    kb = await _seed_kb(db_session, documents=3, chunks_per_document=30)

    with _captured_statements() as seen:
        res = await client.get(
            f"/api/knowledge-bases/{kb.id}/documents", headers=auth_headers()
        )
    assert res.status_code == 200, res.text
    assert _chunk_row_reads(seen) == [], f"document listing touched chunks: {seen}"
    assert len(res.json()) == 3


async def test_kb_collections_raise_instead_of_loading(db_session):
    """``lazy="raise"``: a forgotten explicit query becomes a loud failure."""
    kb = await _seed_kb(db_session, documents=2, chunks_per_document=4)
    doc = (await _docs_of(db_session, kb.id))[0]

    with pytest.raises(InvalidRequestError):
        doc.chunks  # noqa: B018 - the attribute access IS the assertion
    with pytest.raises(InvalidRequestError):
        kb.documents  # noqa: B018


async def test_kb_ownership_lookup_does_not_load_documents(db_session):
    """The per-chat-turn KB lookup (chat_service) reads KB rows only."""
    kb = await _seed_kb(db_session, documents=5, chunks_per_document=20)

    with _captured_statements() as seen:
        rows = (
            (
                await db_session.execute(
                    select(KnowledgeBase).where(KnowledgeBase.id.in_([kb.id]))
                )
            )
            .scalars()
            .all()
        )
    assert [r.id for r in rows] == [kb.id]
    assert _document_row_reads(seen) == []
    assert _chunk_row_reads(seen) == []


async def test_keyword_retriever_emits_exactly_one_chunk_query(db_session):
    """One joined candidate query; no per-document chunk selectin behind it."""
    from app.rag.keyword import KeywordRetriever

    kb = await _seed_kb(db_session, documents=3, chunks_per_document=12, name="lex")
    first = (
        await db_session.execute(
            select(DocumentChunk).where(DocumentChunk.knowledge_base_id == kb.id).limit(1)
        )
    ).scalar_one()
    first.content = "quarterly revenue report quarterly"
    await db_session.commit()

    with _captured_statements() as seen:
        hits = await KeywordRetriever(db_session).retrieve("quarterly revenue", kb.id, top_k=5)
    assert hits, "the retriever should have matched the seeded chunk"
    chunk_stmts = [s for s in seen if "document_chunks" in s.lower()]
    assert len(chunk_stmts) == 1, f"expected one candidate query, got: {chunk_stmts}"


# --------------------------------------------------------------------------- #
# 3: cascade deletes stay complete without the implicit collection load
# --------------------------------------------------------------------------- #
async def test_delete_document_removes_only_its_chunks(client, db_session, no_vectors):
    kb = await _seed_kb(db_session, documents=2, chunks_per_document=6, name="one")
    docs = await _docs_of(db_session, kb.id)
    victim, survivor = docs[0], docs[1]

    res = await client.delete(f"/api/documents/{victim.id}", headers=auth_headers())
    assert res.status_code == 204, res.text
    assert no_vectors, "the document's vectors should have been asked for deletion"

    async with TestSessionLocal() as check:
        assert await check.get(Document, victim.id) is None, "document row survived"
        assert (
            await _count_where(check, DocumentChunk, DocumentChunk.document_id == victim.id)
            == 0
        ), "orphan chunk rows: with lazy='raise' the delete must be explicit"
        assert (
            await _count_where(check, DocumentChunk, DocumentChunk.document_id == survivor.id)
            == 6
        ), "deleting one document must not touch another's chunks"


async def test_delete_kb_removes_documents_and_chunks(client, db_session, no_vectors):
    owner, hdrs = await _fresh_owner(db_session, prefix="kbdel")
    victim = await _seed_kb(
        db_session, user_id=owner.id, documents=3, chunks_per_document=7, name="victim"
    )
    other = await _seed_kb(
        db_session, user_id=owner.id, documents=1, chunks_per_document=2, name="other"
    )

    res = await client.delete(f"/api/knowledge-bases/{victim.id}", headers=hdrs)
    assert res.status_code == 204, res.text
    assert no_vectors, "deleting a KB should still ask Qdrant for its points"

    async with TestSessionLocal() as check:
        assert await check.get(KnowledgeBase, victim.id) is None
        assert (
            await _count_where(check, Document, Document.knowledge_base_id == victim.id) == 0
        ), "orphan documents after the KB delete"
        assert (
            await _count_where(check, DocumentChunk, DocumentChunk.knowledge_base_id == victim.id)
            == 0
        ), "orphan chunks after the KB delete"
        assert (
            await _count_where(check, DocumentChunk, DocumentChunk.knowledge_base_id == other.id)
            == 2
        ), "the other KB must be untouched"

    # And the listing no longer reports the deleted KB.
    body = (await client.get("/api/knowledge-bases", headers=hdrs)).json()
    assert {k["name"] for k in body} == {"other"}


async def test_reindex_clears_old_chunks_without_the_orm_cascade(db_session, no_vectors):
    """``_clear_existing`` is the reindex path's chunk cleanup — must stay explicit."""
    from app.services import document_service

    kb = await _seed_kb(db_session, documents=1, chunks_per_document=3, name="reindex")
    doc = (await _docs_of(db_session, kb.id))[0]

    await document_service._clear_existing(db_session, doc, "kb-collection-placeholder")
    await db_session.commit()

    assert (
        await _count_where(db_session, DocumentChunk, DocumentChunk.document_id == doc.id) == 0
    )
    assert await db_session.get(Document, doc.id) is not None, "the document itself stays"


# --------------------------------------------------------------------------- #
# 4: pagination
# --------------------------------------------------------------------------- #
async def test_kb_list_pages_without_overlap_or_dupes(client, db_session):
    owner, hdrs = await _fresh_owner(db_session, prefix="kbpage")
    for i in range(5):
        await _seed_kb(
            db_session, user_id=owner.id, documents=1, chunks_per_document=1, name=f"kb{i}"
        )

    unpaged = (await client.get("/api/knowledge-bases", headers=hdrs)).json()
    assert len(unpaged) == 5  # backward-compatible default: everything small fits
    all_ids = {k["id"] for k in unpaged}

    pages = []
    for offset in (0, 2, 4):
        r = await client.get(f"/api/knowledge-bases?limit=2&offset={offset}", headers=hdrs)
        assert r.status_code == 200, r.text
        pages.append(r.json())
    assert [len(p) for p in pages] == [2, 2, 1]
    ids = [k["id"] for p in pages for k in p]
    assert len(set(ids)) == 5, "pages overlapped or repeated a row"
    assert set(ids) == all_ids
    # Counts are per KB (GROUP BY), not platform-wide.
    assert all(k["document_count"] == 1 and k["chunk_count"] == 1 for k in pages[0])


async def test_kb_list_rejects_bad_page_params(client, db_session):
    await _seed_kb(db_session)
    for qs in ("limit=0", "limit=501", "offset=-1"):
        res = await client.get(f"/api/knowledge-bases?{qs}", headers=auth_headers())
        assert res.status_code == 422, f"{qs} -> {res.status_code}"


async def test_document_list_pages(client, db_session):
    kb = await _seed_kb(db_session, documents=4, chunks_per_document=2, name="paged")
    p1 = (
        await client.get(
            f"/api/knowledge-bases/{kb.id}/documents?limit=3", headers=auth_headers()
        )
    ).json()
    p2 = (
        await client.get(
            f"/api/knowledge-bases/{kb.id}/documents?limit=3&offset=3", headers=auth_headers()
        )
    ).json()
    assert [len(p) for p in (p1, p2)] == [3, 1]
    assert {d["id"] for d in p1}.isdisjoint({d["id"] for d in p2})
    res = await client.get(
        f"/api/knowledge-bases/{kb.id}/documents?limit=0", headers=auth_headers()
    )
    assert res.status_code == 422


async def test_admin_kb_list_is_scoped_and_paginated(client, db_session):
    """Admins see the platform (paginated); users see only their own rows."""
    suffix = uuid.uuid4().hex[:8]
    foreign = User(
        email=f"kb-owner-{suffix}@example.com",
        username=f"kb-owner-{suffix}",
        password_hash="not-a-real-hash",
        role="user",
        is_active=True,
    )
    db_session.add(foreign)
    await db_session.commit()

    await _seed_kb(db_session, user_id=foreign.id, name="mine-fk")
    await _seed_kb(db_session, documents=1, chunks_per_document=1, name="mine-seeded")

    mine = (
        await client.get("/api/knowledge-bases", headers=auth_headers(get_access_token(foreign.id)))
    ).json()
    assert {k["name"] for k in mine} == {"mine-fk"}

    admin_id = (
        await db_session.execute(select(User.id).where(User.role == "admin"))
    ).scalar_one()
    hdrs = auth_headers(get_access_token(admin_id))
    whole = (await client.get("/api/knowledge-bases?limit=500", headers=hdrs)).json()
    assert {"mine-fk", "mine-seeded"} <= {k["name"] for k in whole}
    one = (await client.get("/api/knowledge-bases?limit=1", headers=hdrs)).json()
    assert len(one) == 1
    # The page's counts stay scoped to the rows actually returned.
    assert {k["id"] for k in one} <= {k["id"] for k in whole}
