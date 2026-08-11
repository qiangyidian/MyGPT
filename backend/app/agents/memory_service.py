"""Task 7: opt-in semantic USER-level long-term memory.

A :class:`MemoryService` extracts candidate facts/preferences, holds them
INACTIVE pending user opt-in, and — on activation — embeds them into a
user-scoped vector collection so the next prompt can retrieve the top-k
relevant memories. Consent, provenance, tenant isolation, correction
(edit/re-embed), and deletion are all surfaced here.

**Pure-core / offline by construction.** The embedding function and the vector
store are injected (``embed_fn`` + ``vector_store``), mirroring
``context_compaction.compact_messages``'s ``summarize_fn`` pattern. Tests inject
deterministic stubs; the production wiring is a thin adapter over the existing
:mod:`app.rag.qdrant_store` + an embedding provider — no live Qdrant/embedding
endpoint is required to unit-test this module.

Tenant isolation: every retrieval is scoped by ``user_id`` (a Qdrant payload
filter); one user's memories never surface for another.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserMemory
from app.rag.base import SearchHit, VectorPoint, VectorStore

logger = logging.getLogger(__name__)

# Default top-k for memory retrieval into the effective prompt.
DEFAULT_TOP_K = 5


class EmbedFn(Protocol):
    def __call__(self, texts: list[str]) -> Awaitable[list[list[float]]]: ...


def _user_or_id(user: Any) -> uuid.UUID:
    """Accept a User row or a raw uuid and return the uuid."""
    if isinstance(user, uuid.UUID):
        return user
    uid = getattr(user, "id", None)
    if uid is not None:
        return uid if isinstance(uid, uuid.UUID) else uuid.UUID(str(uid))
    return uuid.UUID(str(user))


def _is_expired(expires_at, now: datetime) -> bool:
    """Robust expiry check across DB dialects (SQLite returns naive datetimes;
    Postgres returns aware). Treat naive as UTC."""
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return expires_at <= now


class MemoryService:
    """Opt-in semantic user memory: propose → activate → embed → retrieve.

    Construct once with the injected ``embed_fn`` + ``vector_store``; pass a DB
    session to each operation (the service is stateless across requests).
    """

    def __init__(
        self,
        *,
        embed_fn: EmbedFn,
        vector_store: VectorStore,
        collection: str = "user_memories",
    ) -> None:
        self._embed_fn = embed_fn
        self._vector_store = vector_store
        self._collection = collection
        self._ensured = False

    async def _ensure_collection(self, dim: int) -> None:
        if self._ensured:
            return
        await self._vector_store.ensure_collection(self._collection, dim=dim)
        self._ensured = True

    # ------------------------------------------------------------------ #
    # Consent: propose a candidate (inactive by default)
    # ------------------------------------------------------------------ #
    async def propose(
        self,
        db: AsyncSession,
        user: Any,
        content: str,
        *,
        memory_type: str = "fact",
        confidence: float = 0.5,
        source_message_id: uuid.UUID | None = None,
        source_conversation_id: uuid.UUID | None = None,
    ) -> UserMemory:
        """Propose a candidate memory. Always created with ``active=False``.

        Dedupes: if an identical (user_id, content) candidate already exists,
        returns the existing row instead of duplicating.
        """
        user_id = _user_or_id(user)
        content = (content or "").strip()
        if not content:
            raise ValueError("memory content must be non-empty")

        existing = (
            await db.execute(
                select(UserMemory).where(
                    UserMemory.user_id == user_id,
                    UserMemory.content == content,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        memory = UserMemory(
            user_id=user_id,
            memory_type=memory_type,
            content=content,
            confidence=confidence,
            source_message_id=source_message_id,
            source_conversation_id=source_conversation_id,
            active=False,  # opt-in: never active on creation
        )
        db.add(memory)
        await db.flush()
        return memory

    # ------------------------------------------------------------------ #
    # Activate → embed → make retrievable
    # ------------------------------------------------------------------ #
    async def activate(self, db: AsyncSession, memory_id: uuid.UUID) -> UserMemory:
        """Flip a candidate active, embed it, and persist the embedding id."""
        memory = await self._get_owned(db, memory_id)
        # Embed + upsert the vector point.
        vectors = await self._embed_fn([memory.content])
        vector = vectors[0] if vectors else []
        await self._ensure_collection(dim=max(1, len(vector)))
        point_id = (memory.embedding_id or f"um_{memory.id}")
        await self._vector_store.upsert(
            self._collection,
            [
                VectorPoint(
                    id=point_id,
                    vector=vector,
                    payload={
                        "user_id": str(memory.user_id),
                        "memory_id": str(memory.id),
                        "content": memory.content,
                    },
                )
            ],
        )
        memory.active = True
        memory.embedding_id = point_id
        await db.flush()
        return memory

    async def deactivate(self, db: AsyncSession, memory_id: uuid.UUID) -> UserMemory:
        """Flip a memory inactive (removes it from the effective prompt without
        deleting the row). The embedding point is left in place so a later
        ``activate`` is cheap (re-upsert)."""
        memory = await self._get_owned(db, memory_id)
        memory.active = False
        await db.flush()
        return memory

    async def disable(self, db: AsyncSession, user: Any) -> int:
        """Deactivate ALL of a user's memories (e.g. they turned the feature
        off). Returns the number deactivated. Rows are preserved."""
        user_id = _user_or_id(user)
        rows = (
            await db.execute(
                select(UserMemory).where(
                    UserMemory.user_id == user_id,
                    UserMemory.active.is_(True),
                )
            )
        ).scalars().all()
        for m in rows:
            m.active = False
        await db.flush()
        return len(rows)

    # ------------------------------------------------------------------ #
    # Correction: edit re-embeds active memories
    # ------------------------------------------------------------------ #
    async def edit(
        self, db: AsyncSession, memory_id: uuid.UUID, content: str
    ) -> UserMemory:
        """Edit a memory's content. If it was active, re-embed under the new
        text so retrieval reflects the correction."""
        memory = await self._get_owned(db, memory_id)
        content = (content or "").strip()
        if not content:
            raise ValueError("memory content must be non-empty")
        memory.content = content
        if memory.active:
            vectors = await self._embed_fn([content])
            vector = vectors[0] if vectors else []
            await self._ensure_collection(dim=max(1, len(vector)))
            point_id = memory.embedding_id or f"um_{memory.id}"
            await self._vector_store.upsert(
                self._collection,
                [
                    VectorPoint(
                        id=point_id,
                        vector=vector,
                        payload={
                            "user_id": str(memory.user_id),
                            "memory_id": str(memory.id),
                            "content": content,
                        },
                    )
                ],
            )
            memory.embedding_id = point_id
        await db.flush()
        return memory

    # ------------------------------------------------------------------ #
    # Deletion: row + embedding
    # ------------------------------------------------------------------ #
    async def delete(self, db: AsyncSession, memory_id: uuid.UUID) -> None:
        """Delete a memory row and remove its embedding from the vector store."""
        memory = await self._get_owned(db, memory_id)
        embedding_id = memory.embedding_id
        await db.delete(memory)
        await db.flush()
        if embedding_id:
            try:
                await self._vector_store.delete_by_filter(
                    self._collection,
                    {"memory_id": str(memory_id)},
                )
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.warning(
                    "failed to remove embedding for memory %s", memory_id, exc_info=True
                )

    # ------------------------------------------------------------------ #
    # Retrieval: top-k active, non-expired memories for the current prompt
    # ------------------------------------------------------------------ #
    async def retrieve_for_prompt(
        self,
        db: AsyncSession,
        user: Any,
        prompt: str,
        *,
        top_k: int = DEFAULT_TOP_K,
    ) -> list[UserMemory]:
        """Retrieve the top-k active, non-expired memories for this user that
        are semantically closest to ``prompt``. Tenant-isolated by user_id."""
        user_id = _user_or_id(user)
        # Short-circuit: if no active memories exist for the user, skip the
        # embedding round-trip entirely.
        any_active = (
            await db.execute(
                select(UserMemory.id)
                .where(
                    UserMemory.user_id == user_id,
                    UserMemory.active.is_(True),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if any_active is None:
            return []

        vectors = await self._embed_fn([prompt or ""])
        vector = vectors[0] if vectors else []
        await self._ensure_collection(dim=max(1, len(vector)))
        hits: list[SearchHit] = await self._vector_store.search(
            self._collection,
            vector,
            top_k=top_k,
            filters={"user_id": str(user_id)},
        )
        if not hits:
            return []
        hit_ids: dict[str, float] = {}
        for h in hits:
            mid = (h.payload or {}).get("memory_id")
            if mid:
                hit_ids[mid] = h.score
        # Load the rows, drop expired ones, order by retrieval score.
        rows = (
            await db.execute(
                select(UserMemory).where(
                    UserMemory.id.in_([uuid.UUID(mid) for mid in hit_ids]),
                    UserMemory.active.is_(True),
                )
            )
        ).scalars().all()
        now = datetime.now(timezone.utc)
        rows = [r for r in rows if not _is_expired(r.expires_at, now)]
        rows.sort(key=lambda r: hit_ids.get(str(r.id), 0.0), reverse=True)
        return rows[:top_k]

    async def list_active_contents(
        self, db: AsyncSession, user: Any, *, limit: int = 50
    ) -> list[str]:
        """Return the plain-text contents of a user's active, non-expired
        memories — the list folded into the effective system prompt."""
        user_id = _user_or_id(user)
        rows = (
            await db.execute(
                select(UserMemory)
                .where(
                    UserMemory.user_id == user_id,
                    UserMemory.active.is_(True),
                )
                .order_by(desc(UserMemory.updated_at))
                .limit(limit)
            )
        ).scalars().all()
        now = datetime.now(timezone.utc)
        return [r.content for r in rows if not _is_expired(r.expires_at, now)]

    # ------------------------------------------------------------------ #
    async def _get_owned(self, db: AsyncSession, memory_id: uuid.UUID) -> UserMemory:
        memory = await db.get(UserMemory, memory_id)
        if memory is None:
            raise KeyError(f"UserMemory {memory_id} not found")
        return memory


__all__ = ["MemoryService", "EmbedFn"]
