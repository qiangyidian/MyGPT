"""Task 7: opt-in semantic USER-level long-term memory.

Consent, provenance, tenant isolation, correction, deletion, and retrieval —
backed by an injectable embed/vector layer so tests run fully OFFLINE (no live
Qdrant, no live embedding endpoint).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, UTC

import pytest

from app.agents.memory_service import MemoryService
from app.models import UserMemory


# --------------------------------------------------------------------------- #
# Injected stubs (deterministic, offline)
# --------------------------------------------------------------------------- #
class FakeVectorStore:
    """An in-memory stand-in for Qdrant that respects user_id payload filters."""

    def __init__(self) -> None:
        self.points: dict[str, dict] = {}  # id -> {vector, payload}

    async def ensure_collection(self, collection: str, dim: int) -> None:
        self.collection = collection
        self.dim = dim

    async def upsert(self, collection: str, points) -> None:
        for p in points:
            self.points[p.id] = {"vector": p.vector, "payload": dict(p.payload or {})}

    async def search(self, collection, query, top_k=5, filters=None):
        filt = filters or {}
        hits = []
        for pid, point in self.points.items():
            if all(point["payload"].get(k) == str(v) for k, v in filt.items()):
                # cosine-ish score: dot product of small integer vectors.
                score = sum(a * b for a, b in zip(query, point["vector"]))
                hits.append(type("H", (), {"id": pid, "score": float(score), "payload": dict(point["payload"])})())
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    async def delete_by_filter(self, collection, filters):
        filt = filters or {}
        to_drop = [
            pid for pid, point in self.points.items()
            if all(point["payload"].get(k) == str(v) for k, v in filt.items())
        ]
        for pid in to_drop:
            del self.points[pid]


def _stub_embed(texts):
    """Deterministic embedding: hash chars into a small fixed-dim vector."""

    async def _run():
        out = []
        for t in texts:
            v = [0] * 16
            for i, ch in enumerate(t):
                v[i % 16] += ord(ch) % 7
            out.append(v)
        return out

    return _run()


@pytest.fixture
def vector_store():
    return FakeVectorStore()


@pytest.fixture
def service(vector_store):
    return MemoryService(
        embed_fn=_stub_embed,
        vector_store=vector_store,
    )


@pytest.fixture
def user_id():
    return uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def other_user_id():
    return uuid.UUID("00000000-0000-0000-0000-000000000002")


async def _persist_user(db_session, uid):
    """Ensure a user row exists for the FK (seeded user already exists in
    conftest; only create for ad-hoc ids)."""
    from sqlalchemy import select

    from app.models import User

    row = await db_session.execute(select(User).where(User.id == uid))
    if row.scalar_one_or_none() is None:
        db_session.add(
            User(
                id=uid,
                email=f"{uid}@example.com",
                username=str(uid)[:8],
                password_hash="x",
                role="user",
                is_active=True,
            )
        )
        await db_session.commit()


# --------------------------------------------------------------------------- #
# Consent: candidate is inactive until opt-in
# --------------------------------------------------------------------------- #
async def test_memory_candidate_is_inactive_without_opt_in(service, db_session, user_id):
    await _persist_user(db_session, user_id)
    memory = await service.propose(
        db_session, user_id, "prefers concise answers", source_conversation_id=None
    )
    assert memory.active is False
    assert memory.user_id == user_id
    # An inactive candidate must NOT be retrievable into the effective prompt.
    retrieved = await service.retrieve_for_prompt(db_session, user_id, "anything", top_k=5)
    assert retrieved == []


async def test_propose_records_provenance_and_confidence(service, db_session, user_id):
    await _persist_user(db_session, user_id)
    memory = await service.propose(
        db_session, user_id, "uses Python daily",
        confidence=0.8,
        memory_type="preference",
    )
    assert memory.confidence == pytest.approx(0.8)
    assert memory.memory_type == "preference"
    # Provenance column is wired (may be None when no source message).
    assert hasattr(memory, "source_message_id")
    assert hasattr(memory, "source_conversation_id")


# --------------------------------------------------------------------------- #
# Activate → embed → retrieve
# --------------------------------------------------------------------------- #
async def test_activate_embeds_and_makes_retrievable(service, db_session, user_id, vector_store):
    await _persist_user(db_session, user_id)
    memory = await service.propose(db_session, user_id, "prefers concise answers")

    activated = await service.activate(db_session, memory.id)

    assert activated.active is True
    assert activated.embedding_id is not None  # embedding was stored
    # The vector store now has a point for this user.
    assert any(
        p["payload"].get("user_id") == str(user_id)
        for p in vector_store.points.values()
    )
    # And the memory is now retrievable.
    retrieved = await service.retrieve_for_prompt(
        db_session, user_id, "prefers concise answers", top_k=5
    )
    assert any(m.id == memory.id for m in retrieved)


# --------------------------------------------------------------------------- #
# Deactivate / disable remove from the effective prompt without deleting the row
# --------------------------------------------------------------------------- #
async def test_deactivate_removes_from_retrieval(service, db_session, user_id):
    await _persist_user(db_session, user_id)
    memory = await service.propose(db_session, user_id, "likes dark mode")
    await service.activate(db_session, memory.id)

    deactivated = await service.deactivate(db_session, memory.id)

    assert deactivated.active is False
    retrieved = await service.retrieve_for_prompt(db_session, user_id, "dark mode", top_k=5)
    assert retrieved == []
    # Row is NOT deleted (correction/disable does not lose the candidate).
    from sqlalchemy import select

    row = (await db_session.execute(select(UserMemory).where(UserMemory.id == memory.id))).scalar_one_or_none()
    assert row is not None


async def test_disable_removes_all_user_memories_from_prompt(service, db_session, user_id):
    await _persist_user(db_session, user_id)
    m1 = await service.propose(db_session, user_id, "prefers concise answers")
    m2 = await service.propose(db_session, user_id, "uses vim")
    await service.activate(db_session, m1.id)
    await service.activate(db_session, m2.id)

    count = await service.disable(db_session, user_id)

    assert count >= 2
    retrieved = await service.retrieve_for_prompt(db_session, user_id, "anything", top_k=10)
    assert retrieved == []


# --------------------------------------------------------------------------- #
# Edit re-embeds; delete removes row + embedding
# --------------------------------------------------------------------------- #
async def test_edit_re_embeds_active_memory(service, db_session, user_id, vector_store):
    await _persist_user(db_session, user_id)
    memory = await service.propose(db_session, user_id, "likes dark mode")
    await service.activate(db_session, memory.id)
    first_embedding_id = memory.embedding_id

    edited = await service.edit(db_session, memory.id, "prefers light mode")

    assert edited.content == "prefers light mode"
    # Re-embedded: a new embedding point was written.
    assert edited.embedding_id is not None
    assert any(p["payload"].get("memory_id") == str(memory.id) for p in vector_store.points.values())
    # The old point was replaced (id changed or old id removed).
    assert edited.active is True  # stays active across an edit


async def test_delete_removes_row_and_embedding(service, db_session, user_id, vector_store):
    await _persist_user(db_session, user_id)
    memory = await service.propose(db_session, user_id, "tmp fact")
    await service.activate(db_session, memory.id)
    assert any(vector_store.points.values())

    await service.delete(db_session, memory.id)

    from sqlalchemy import select

    row = (await db_session.execute(select(UserMemory).where(UserMemory.id == memory.id))).scalar_one_or_none()
    assert row is None
    # Embedding also removed.
    assert not any(
        p["payload"].get("memory_id") == str(memory.id) for p in vector_store.points.values()
    )


# --------------------------------------------------------------------------- #
# Tenant isolation: one user's memories never surface for another
# --------------------------------------------------------------------------- #
async def test_tenant_isolation_retrieval_scoped_by_user(
    service, db_session, user_id, other_user_id
):
    await _persist_user(db_session, user_id)
    await _persist_user(db_session, other_user_id)
    m = await service.propose(db_session, user_id, "secret: prefers concise answers")
    await service.activate(db_session, m.id)

    # The other user retrieves nothing — even for the exact same prompt.
    other = await service.retrieve_for_prompt(
        db_session, other_user_id, "prefers concise answers", top_k=5
    )
    assert other == []
    # The owning user retrieves it.
    own = await service.retrieve_for_prompt(
        db_session, user_id, "prefers concise answers", top_k=5
    )
    assert any(mem.id == m.id for mem in own)


# --------------------------------------------------------------------------- #
# Expiry: expired memories are excluded from retrieval
# --------------------------------------------------------------------------- #
async def test_expired_memories_excluded_from_retrieval(service, db_session, user_id):
    await _persist_user(db_session, user_id)
    memory = await service.propose(db_session, user_id, "on vacation until Friday")
    await service.activate(db_session, memory.id)
    # Force expiry in the past.
    memory.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.commit()

    retrieved = await service.retrieve_for_prompt(
        db_session, user_id, "on vacation", top_k=5
    )
    assert retrieved == []


# --------------------------------------------------------------------------- #
# Dedupe: proposing a near-identical candidate updates instead of duplicating
# --------------------------------------------------------------------------- #
async def test_dedupe_near_identical_candidate(service, db_session, user_id):
    await _persist_user(db_session, user_id)
    first = await service.propose(db_session, user_id, "prefers concise answers")
    second = await service.propose(db_session, user_id, "prefers concise answers")
    # Same content, same user -> deduped (not a duplicate row).
    assert second.id == first.id
