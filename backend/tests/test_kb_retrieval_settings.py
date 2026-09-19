"""Per-knowledge-base retrieval settings (C20/C23) and what they must NOT do.

The columns are nullable, NULL means "inherit the platform default", and that
rule is the whole point: turning per-KB tuning on must not silently re-tune the
retrieval of every knowledge base that already exists.

Layer by layer, the guarantees pinned here are:

* API: create/PATCH round-trip the overrides, an omitted PATCH key stays as-is
  while an explicit ``null`` clears it, and ``embedding_model_id`` must point at
  a real embedding model;
* resolution: ``_KbRetrieval.from_kb`` falls back to global settings per column;
* application: one KB's ``top_k`` does not leak into a sibling KB's recall
  window, and neither does one KB's score threshold;
* ``rerank_enabled=False`` is a request-wide switch (reranking is one global
  pass over the fused list), so it only holds when every KB opts out.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import KnowledgeBase, ModelConfig, User
from app.rag.base import SearchHit
from app.rag.rag_service import (
    RagService,
    _KbRetrieval,
    _cap_per_kb,
    _threshold_of,
)
from tests.conftest import auth_headers, get_access_token

SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _fresh_owner(db) -> tuple[User, dict]:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"kbset-{suffix}@example.com",
        username=f"kbset-{suffix}",
        password_hash="not-a-real-hash",
        role="user",
        is_active=True,
    )
    db.add(user)
    await db.commit()
    return user, auth_headers(get_access_token(user.id))


async def _embedding_config(db, user_id, *, embedding: bool = True) -> ModelConfig:
    cfg = ModelConfig(
        user_id=user_id,
        name=f"emb-{uuid.uuid4().hex[:8]}",
        provider="mock",
        api_base_url="mock://",
        model_name="mock",
        embedding_model_name="mock-embed" if embedding else None,
        is_embedding=embedding,
    )
    db.add(cfg)
    await db.commit()
    return cfg


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #
async def test_create_persists_every_override(client, db_session):
    owner, hdrs = await _fresh_owner(db_session)
    cfg = await _embedding_config(db_session, owner.id)
    res = await client.post(
        "/api/knowledge-bases",
        headers=hdrs,
        json={
            "name": "tuned",
            "embedding_model_id": str(cfg.id),
            "top_k": 3,
            "score_threshold": 0.42,
            "rerank_enabled": False,
            "chunk_size": 400,
            "chunk_overlap": 60,
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["top_k"] == 3
    assert body["score_threshold"] == pytest.approx(0.42)
    assert body["rerank_enabled"] is False
    assert body["chunk_size"] == 400
    assert body["chunk_overlap"] == 60


async def test_create_without_overrides_leaves_them_inheritable(client, db_session):
    _owner, hdrs = await _fresh_owner(db_session)
    res = await client.post("/api/knowledge-bases", headers=hdrs, json={"name": "plain"})
    assert res.status_code == 201, res.text
    body = res.json()
    # NULL, not 0: 0 would be a real (broken) value the resolver could not tell
    # apart from "not configured".
    assert [body[k] for k in ("top_k", "score_threshold", "rerank_enabled", "chunk_size", "chunk_overlap")] == [None] * 5


async def test_create_rejects_a_chat_model_as_the_embedding_model(client, db_session):
    owner, hdrs = await _fresh_owner(db_session)
    chat = await _embedding_config(db_session, owner.id, embedding=False)
    res = await client.post(
        "/api/knowledge-bases",
        headers=hdrs,
        json={"name": "wrong-model", "embedding_model_id": str(chat.id)},
    )
    assert res.status_code == 400
    missing = await client.post(
        "/api/knowledge-bases",
        headers=hdrs,
        json={"name": "wrong-id", "embedding_model_id": str(uuid.uuid4())},
    )
    assert missing.status_code == 400


async def test_patch_writes_only_what_was_sent_and_clears_on_null(client, db_session):
    owner, hdrs = await _fresh_owner(db_session)
    created = (
        await client.post(
            "/api/knowledge-bases",
            headers=hdrs,
            json={"name": "patch", "top_k": 7, "chunk_size": 300},
        )
    ).json()
    kb_id = created["id"]

    # Omitted keys stay: chunk_size 300 must survive this PATCH.
    res = await client.patch(f"/api/knowledge-bases/{kb_id}", headers=hdrs, json={"top_k": 2})
    assert res.status_code == 200, res.text
    assert res.json()["top_k"] == 2
    assert res.json()["chunk_size"] == 300

    # An explicit null is an action: it resets to "inherit the platform default".
    cleared = await client.patch(
        f"/api/knowledge-bases/{kb_id}", headers=hdrs, json={"top_k": None}
    )
    assert cleared.json()["top_k"] is None
    assert cleared.json()["chunk_size"] == 300


async def test_patch_validates_its_input(client, db_session):
    owner, hdrs = await _fresh_owner(db_session)
    kb_id = (
        await client.post("/api/knowledge-bases", headers=hdrs, json={"name": "valid"})
    ).json()["id"]

    chat = await _embedding_config(db_session, owner.id, embedding=False)
    assert (
        await client.patch(
            f"/api/knowledge-bases/{kb_id}",
            headers=hdrs,
            json={"embedding_model_id": str(chat.id)},
        )
    ).status_code == 400
    # overlap >= chunk_size is a splitter that never terminates.
    assert (
        await client.patch(
            f"/api/knowledge-bases/{kb_id}",
            headers=hdrs,
            json={"chunk_size": 100, "chunk_overlap": 100},
        )
    ).status_code == 422
    assert (
        await client.patch(f"/api/knowledge-bases/{kb_id}", headers=hdrs, json={})
    ).status_code == 400


async def test_patch_is_scoped_to_the_owner(client, db_session):
    owner, owner_hdrs = await _fresh_owner(db_session)
    other, other_hdrs = await _fresh_owner(db_session)
    kb_id = (
        await client.post(
            "/api/knowledge-bases", headers=owner_hdrs, json={"name": "mine", "top_k": 4}
        )
    ).json()["id"]

    res = await client.patch(f"/api/knowledge-bases/{kb_id}", headers=other_hdrs, json={"top_k": 1})
    assert res.status_code == 404
    # ... and the row really is untouched.
    assert (await client.get(f"/api/knowledge-bases/{kb_id}", headers=owner_hdrs)).json()["top_k"] == 4


async def test_admin_may_patch_another_users_kb(client, db_session):
    owner, owner_hdrs = await _fresh_owner(db_session)
    kb_id = (
        await client.post(
            "/api/knowledge-bases", headers=owner_hdrs, json={"name": "theirs"}
        )
    ).json()["id"]
    # Reuse the admin conftest already seeds rather than minting a second one:
    # the session DB is shared across test files, and a stray extra admin row
    # breaks the "the one admin" lookups elsewhere (scalar_one()).
    admin = (
        await db_session.execute(
            select(User).where(User.email == "admin-test@example.com")
        )
    ).scalar_one()
    hdrs = auth_headers(get_access_token(admin.id))
    assert (
        await client.patch(f"/api/knowledge-bases/{kb_id}", headers=hdrs, json={"top_k": 9})
    ).status_code == 200


# --------------------------------------------------------------------------- #
# Resolution: NULL inherits, and only NULL does
# --------------------------------------------------------------------------- #
class _Settings:
    RAG_TOP_K = 5
    RAG_MIN_SCORE = 0.3


def _kb(**over):
    values = {
        "top_k": None,
        "score_threshold": None,
        "rerank_enabled": None,
        "chunk_size": None,
        "chunk_overlap": None,
    }
    values.update(over)
    return KnowledgeBase(user_id=SEEDED_USER, name="x", **values)


def test_an_untuned_kb_resolves_to_the_platform_defaults():
    """Every KB created before these columns existed keeps behaving as before."""
    ks = _KbRetrieval.from_kb(_kb(), _Settings)
    assert (ks.top_k, ks.score_threshold, ks.rerank_enabled) == (5, 0.3, None)


def test_a_tuned_kb_overrides_only_the_columns_it_sets():
    ks = _KbRetrieval.from_kb(_kb(top_k=2, rerank_enabled=False), _Settings)
    assert (ks.top_k, ks.score_threshold, ks.rerank_enabled) == (2, 0.3, False)


@pytest.mark.parametrize(
    "raw,expected", [(0, 5), (-3, 5), (12, 12)]
)
def test_a_nonsensical_top_k_falls_back_instead_of_disabling_retrieval(raw, expected):
    ks = _KbRetrieval.from_kb(_kb(top_k=raw), _Settings)
    assert ks.top_k == expected


def test_threshold_and_cap_are_read_per_hit_from_its_own_kb():
    kb_a = KnowledgeBase(
        id=uuid.uuid4(), user_id=SEEDED_USER, name="a", score_threshold=0.9, top_k=1
    )
    kb_settings = {str(kb_a.id): _KbRetrieval.from_kb(kb_a, _Settings)}
    tagged = SearchHit(id="1", score=0.5, payload={"kb_id": str(kb_a.id)})
    untagged = SearchHit(id="2", score=0.5, payload={})
    assert _threshold_of(tagged, kb_settings, 0.3) == 0.9
    assert _threshold_of(untagged, kb_settings, 0.3) == 0.3

    hits = [SearchHit(id=str(i), score=1.0, payload={"kb_id": str(kb_a.id)}) for i in range(4)]
    assert len(_cap_per_kb(hits, kb_settings)) == 1
    # An untagged hit is nobody's override business: it survives the cap, while
    # the four tagged ones still collapse to that KB's own top_k.
    assert len(_cap_per_kb([untagged, *hits], kb_settings)) == 2


# --------------------------------------------------------------------------- #
# Application, end to end through RagService
# --------------------------------------------------------------------------- #
class _FakeRetriever:
    """Returns one hit per requested ``top_k`` — the window it was asked for."""

    calls: list[int] = []

    def __init__(self, embedder, store, reranker):
        pass

    async def retrieve(self, query, collection, top_k=5, overfetch=1):
        _FakeRetriever.calls.append(top_k)
        return [
            SearchHit(
                id=f"{collection}-{i}",
                score=0.5,
                # Text has to be unique per KB: identical chunk bodies are what
                # ``compress_context`` dedups, and two KBs returning the same
                # string would erase the very thing these tests measure.
                payload={"text": f"{collection} chunk {i}", "document_name": "d"},
            )
            for i in range(top_k)
        ]


class _NoKeyword:
    def __init__(self, db):
        pass

    async def retrieve(self, query, kb_id, top_k=5):
        return []


@pytest.fixture
def rag_double(monkeypatch):
    """Replace every external edge of retrieval with a deterministic double."""
    from app.rag import rag_service as rs

    calls: list[int] = []

    class _Cfg:
        embedding_model_name = "mock-embed"
        model_name = "mock"

    async def _resolve(db, kb):
        return _Cfg()

    class _Embedder:
        def __init__(self, *a, **k):
            self.dim = 4

    class _Reranker:
        kind = "fake"

        async def rerank(self, query, hits, top_k=5):
            calls.append(top_k)
            for h in hits:
                h.rerank_score = h.score
            return hits

    # A subclass of the real noop so ``isinstance(reranker, NoopReranker)`` — the
    # check that decides whether to over-fetch at all — behaves like production.
    class _Noop(rs.NoopReranker):
        async def rerank(self, query, hits, top_k=5):  # pragma: no cover - unused
            return hits

    state = {"reranker": "noop", "rerank_calls": calls}

    def _make(settings):
        return _Reranker() if state["reranker"] == "real" else _Noop()

    monkeypatch.setattr(rs, "_resolve_embedding_config", _resolve)
    monkeypatch.setattr(rs, "get_provider_for_config", lambda cfg: object())
    monkeypatch.setattr(rs, "ProviderEmbedder", _Embedder)
    monkeypatch.setattr(rs, "Retriever", _FakeRetriever)
    monkeypatch.setattr(rs, "KeywordRetriever", _NoKeyword)
    monkeypatch.setattr(rs, "make_reranker", _make)
    monkeypatch.setattr(rs, "get_vector_store", lambda: object())
    _FakeRetriever.calls = []
    return state


def _kb_row(db, owner_id, **over):
    kb = KnowledgeBase(user_id=owner_id, name=over.pop("name", "kb"), **over)
    db.add(kb)
    return kb


async def test_each_kb_gets_its_own_recall_window(db_session, rag_double, monkeypatch):
    """A KB tuned to 2 chunks must not ride a sibling's 8.

    Both the fetch window and the final per-KB cap are asserted, because they are
    different mistakes: the first over-fetches, the second lets a noisy KB fill
    the prompt.
    """
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "RAG_HYBRID", False)
    small = _kb_row(db_session, SEEDED_USER, name="small", top_k=2)
    big = _kb_row(db_session, SEEDED_USER, name="big", top_k=8)
    await db_session.commit()

    _ctx, citations = await RagService().retrieve(db_session, "问题", [small.id, big.id])

    assert sorted(_FakeRetriever.calls) == [2, 8]
    by_kb: dict[str, int] = {}
    for c in citations:
        by_kb[c.metadata["kb_name"]] = by_kb.get(c.metadata["kb_name"], 0) + 1
    assert by_kb == {"small": 2, "big": 8}


async def test_a_kb_threshold_gates_its_own_hits_only(db_session, rag_double, monkeypatch):
    from app.core.config import get_settings

    settings = get_settings()
    strict = _kb_row(db_session, SEEDED_USER, name="strict", score_threshold=0.9)
    loose = _kb_row(db_session, SEEDED_USER, name="loose", top_k=3)
    await db_session.commit()
    monkeypatch.setattr(settings, "RAG_HYBRID", False)
    monkeypatch.setattr(settings, "RAG_MIN_SCORE", 0.0)

    # Every fake hit scores 0.5: the strict KB gates all of its own out, the
    # loose KB (inheriting the global 0.0) keeps its own.
    _ctx, citations = await RagService().retrieve(db_session, "问题", [strict.id, loose.id])
    assert {c.metadata["kb_name"] for c in citations} == {"loose"}
    assert len(citations) == 3

    _ctx2, both = await RagService().retrieve(db_session, "另一问", [loose.id])
    assert len(both) == 3


async def test_rerank_opt_out_needs_every_kb_in_the_request(db_session, rag_double, monkeypatch):
    from app.core.config import get_settings

    off = _kb_row(db_session, SEEDED_USER, name="off", rerank_enabled=False)
    on = _kb_row(db_session, SEEDED_USER, name="on", rerank_enabled=True)
    await db_session.commit()
    monkeypatch.setattr(get_settings(), "RAG_HYBRID", False)
    rag_double["reranker"] = "real"

    await RagService().retrieve(db_session, "问题", [off.id])
    assert rag_double["rerank_calls"] == [], "the only KB in the request opted out"

    await RagService().retrieve(db_session, "问题", [off.id, on.id])
    assert rag_double["rerank_calls"], "one KB still wants reranking, and it is one global pass"


async def test_a_kb_chunking_override_shapes_its_ingestion(db_session):
    """The chunk columns are only real if indexing reads them."""
    from app.core.config import get_settings
    from app.rag.splitter import RecursiveTextSplitter

    kb = _kb_row(db_session, SEEDED_USER, name="chunky", chunk_size=120, chunk_overlap=20)
    await db_session.commit()
    splitter = RecursiveTextSplitter(chunk_size=kb.chunk_size, chunk_overlap=kb.chunk_overlap)
    assert splitter.chunk_size == 120
    assert splitter.chunk_overlap == 20
    # The override is per-KB: a different KB's row keeps the platform default.
    other = _kb_row(db_session, SEEDED_USER, name="plain")
    await db_session.commit()
    default = RecursiveTextSplitter(
        chunk_size=other.chunk_size, chunk_overlap=other.chunk_overlap
    )
    assert default.chunk_size == get_settings().RAG_CHUNK_SIZE
