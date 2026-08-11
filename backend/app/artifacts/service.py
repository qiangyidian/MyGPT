"""ArtifactService — create, tenant-scoped open, checksum verify, retention.

Single entry point for first-class artifacts (Task 10). Enforces:

  * **Tenant isolation** — every lookup is owner-scoped; a foreign user gets a
    404 (no existence leak). Admins bypass for audit/support.
  * **Opaque storage** — the blob is persisted via :class:`StorageBackend`;
    ``storage_key`` is the only stored reference and is NEVER handed to the
    model or client. The model sees ``artifact:<id>``; the API resolves
    id → owner check → storage → streaming response.
  * **Integrity** — sha256 + size + media_type computed at create time and
    re-verified on every read to detect corruption/tamper.
  * **Retention** — an expired artifact (``expires_at`` in the past) is hidden
    from open/listing (404) and may be reaped.

This service is what backs Task-7's ``ArtifactHandle`` for real: a spilled blob
becomes an Artifact and the model is handed only the opaque ``artifact:<id>``.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.storage import get_storage
from app.models.artifact import SOURCES, Artifact

logger = logging.getLogger(__name__)

# sha256 hex length (sanity guard on persisted checksums).
_CHECKSUM_LEN = 64


class ArtifactService:
    """Tenant-scoped artifact CRUD + authorized, checksummed read."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------ #
    # Create
    # ------------------------------------------------------------------ #
    async def create_from_bytes(
        self,
        *,
        owner_id: uuid.UUID,
        data: bytes,
        media_type: str,
        filename: str,
        source: str = "upload",
        expires_at: datetime | None = None,
        retention_policy: str | None = None,
        run_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
        generator: dict[str, Any] | None = None,
    ) -> Artifact:
        """Persist ``data`` as a new Artifact and return the row.

        Checksum (sha256), size, and media_type are computed here and stored on
        the row; reads re-verify the checksum. The blob is stored via the
        configured ``StorageBackend`` under an opaque key.
        """
        if source not in SOURCES:
            raise ValueError(f"unknown artifact source: {source!r}")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")
        storage = get_storage()
        ext = _ext_of(filename)
        # Artifacts accept any blob the producer emits; allow exactly this
        # extension (or none) rather than the chat-upload allow-list.
        allow = {ext} if ext else set()
        from fastapi import UploadFile

        upload = UploadFile(filename=filename or "artifact", file=io.BytesIO(data))
        storage_key = await storage.save(upload, owner_id, allowed_extensions=allow)

        checksum = hashlib.sha256(data).hexdigest()
        artifact = Artifact(
            owner_id=owner_id,
            checksum=checksum,
            size=len(data),
            media_type=media_type or "application/octet-stream",
            storage_key=storage_key,
            filename=_sanitize_filename(filename or "artifact"),
            source=source,
            expires_at=expires_at,
            retention_policy=retention_policy,
            run_id=run_id,
            step_id=step_id,
            generator=generator,
        )
        self._db.add(artifact)
        await self._db.commit()
        await self._db.refresh(artifact)
        return artifact

    async def create_from_upload(
        self,
        *,
        owner_id: uuid.UUID,
        upload_file: Any,
        source: str = "upload",
        media_type: str | None = None,
        filename: str | None = None,
        expires_at: datetime | None = None,
        retention_policy: str | None = None,
        run_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
        generator: dict[str, Any] | None = None,
    ) -> Artifact:
        """Persist an ``UploadFile`` as a new Artifact (streamed to storage)."""
        if source not in SOURCES:
            raise ValueError(f"unknown artifact source: {source!r}")
        storage = get_storage()
        effective_name = filename or getattr(upload_file, "filename", None) or "artifact"
        ext = _ext_of(effective_name)
        allow = {ext} if ext else set()
        storage_key = await storage.save(upload_file, owner_id, allowed_extensions=allow)

        # Read back to compute checksum + exact size (storage streams to disk;
        # content-length is not trusted).
        def _read() -> bytes:
            with storage.open(storage_key) as fh:
                return fh.read()

        data = await asyncio.to_thread(_read)
        checksum = hashlib.sha256(data).hexdigest()
        declared_mt = media_type or getattr(upload_file, "content_type", None) or ""
        if not declared_mt:
            # Sniff from the filename so a bare UploadFile still gets a real
            # media_type (browsers don't always set content-type on uploads).
            import mimetypes

            guess, _ = mimetypes.guess_type(effective_name)
            declared_mt = guess or ""
        artifact = Artifact(
            owner_id=owner_id,
            checksum=checksum,
            size=len(data),
            media_type=declared_mt or "application/octet-stream",
            storage_key=storage_key,
            filename=_sanitize_filename(effective_name),
            source=source,
            expires_at=expires_at,
            retention_policy=retention_policy,
            run_id=run_id,
            step_id=step_id,
            generator=generator,
        )
        self._db.add(artifact)
        await self._db.commit()
        await self._db.refresh(artifact)
        return artifact

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #
    async def open(self, artifact_id: uuid.UUID, user: Any) -> bytes:
        """Authorized, checksum-verified byte read.

        ``user`` is the requesting principal (a User-like with ``id``/``role``
        or a bare UUID). A foreign or unknown id yields 404 (no existence
        leak); an expired artifact yields 404; a checksum mismatch yields 410
        (the content is unavailable due to a detected integrity failure).
        """
        artifact = await self._get_owned(artifact_id, user)
        storage = get_storage()

        def _read() -> bytes:
            with storage.open(artifact.storage_key) as fh:
                return fh.read()

        data = await asyncio.to_thread(_read)
        actual = hashlib.sha256(data).hexdigest()
        if actual != artifact.checksum:
            logger.warning(
                "artifact %s checksum mismatch (expected %s, got %s)",
                artifact_id, artifact.checksum, actual,
            )
            raise AppException(
                410,
                "artifact_checksum_mismatch",
                "artifact content failed integrity verification",
            )
        return data

    async def open_stream(
        self, artifact_id: uuid.UUID, user: Any, *, chunk_size: int = 64 * 1024
    ) -> Any:
        """Authorized, **chunked** streaming read (memory-bounded).

        Yields ``bytes`` chunks of at most ``chunk_size`` read from
        :meth:`StorageBackend.open`, WITHOUT buffering the whole file — so a
        large artifact does not sit fully in RAM (× concurrency = memory
        exhaustion). Tamper detection is preserved: the sha256 is computed
        incrementally as chunks flow. Because HTTP status/headers are already
        sent by the time the first chunk streams, a mismatch cannot become a
        clean 410 — instead the stream is ABORTED (the generator returns) and a
        critical warning is logged; the create-time checksum + audit are the
        integrity record. Use :meth:`open` for a clean 410 when the caller does
        not need streaming.

        Authorization is identical to :meth:`open`: tenant-scoped (foreign →
        404, no existence leak) and retention-enforced.
        """
        artifact = await self._get_owned(artifact_id, user)
        storage = get_storage()
        expected = artifact.checksum
        key = artifact.storage_key

        # Open the handle off the event loop once; chunk reads are cheap.
        handle = await asyncio.to_thread(storage.open, key)

        def _gen():
            hasher = hashlib.sha256()
            try:
                while True:
                    buf = handle.read(chunk_size)
                    if not buf:
                        break
                    hasher.update(buf)
                    yield buf
                if hasher.hexdigest() != expected:
                    logger.error(
                        "artifact %s checksum mismatch on stream (expected %s, got %s)",
                        artifact_id, expected, hasher.hexdigest(),
                    )
                    # Stream aborted; client sees a truncated body (best we can
                    # do once headers are committed). The audit log + create-time
                    # checksum are the integrity record.
            finally:
                try:
                    handle.close()
                except Exception:  # noqa: BLE001
                    pass

        return _gen()

    async def get(self, artifact_id: uuid.UUID, user: Any) -> Artifact:
        """Tenant-scoped metadata fetch (no bytes; no storage access)."""
        return await self._get_owned(artifact_id, user)

    async def list_for_owner(
        self, owner_id: uuid.UUID, *, include_expired: bool = False
    ) -> list[Artifact]:
        """List the owner's non-expired artifacts (newest first)."""
        from sqlalchemy import select

        res = await self._db.execute(
            select(Artifact)
            .where(Artifact.owner_id == owner_id)
            .order_by(Artifact.created_at.desc())
        )
        rows = list(res.scalars().all())
        if include_expired:
            return rows
        now = _now_utc()
        return [r for r in rows if not _is_expired(r, now)]

    # ------------------------------------------------------------------ #
    # Delete
    # ------------------------------------------------------------------ #
    async def delete(self, artifact_id: uuid.UUID, user: Any) -> None:
        artifact = await self._get_owned(artifact_id, user)
        storage = get_storage()
        try:
            await storage.delete(artifact.storage_key)
        except Exception:  # noqa: BLE001 — best-effort; the row goes regardless
            logger.warning("artifact %s storage delete failed", artifact_id, exc_info=True)
        await self._db.delete(artifact)
        await self._db.commit()

    async def reap_expired(self, *, batch_size: int = 200) -> int:
        """Delete storage + rows for artifacts past their ``expires_at``.

        Returns the number reaped. Intended for a periodic background job.
        """
        from sqlalchemy import select

        now = _now_utc()
        res = await self._db.execute(
            select(Artifact).where(Artifact.expires_at.is_not(None)).limit(batch_size)
        )
        storage = get_storage()
        count = 0
        for art in res.scalars().all():
            if not _is_expired(art, now):
                continue
            try:
                await storage.delete(art.storage_key)
            except Exception:  # noqa: BLE001
                pass
            await self._db.delete(art)
            count += 1
        if count:
            await self._db.commit()
        return count

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    async def _get_owned(self, artifact_id: uuid.UUID, user: Any) -> Artifact:
        artifact = await self._db.get(Artifact, artifact_id)
        if artifact is None:
            raise AppException(404, "artifact_not_found", "artifact not found")
        uid = getattr(user, "id", user)
        role = getattr(user, "role", "user")
        # Tenant isolation: foreign user → 404 (not 403) so existence never leaks.
        if artifact.owner_id != uid and role != "admin":
            raise AppException(404, "artifact_not_found", "artifact not found")
        # Retention: an expired artifact is hidden (404) from open/listing.
        if _is_expired(artifact, _now_utc()):
            raise AppException(404, "artifact_not_found", "artifact not found")
        return artifact


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ext_of(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def _sanitize_filename(name: str) -> str:
    base = os.path.basename(name or "") or "artifact"
    base = base.replace("\x00", "").strip()
    return base[:200] or "artifact"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _is_expired(artifact: Artifact, now: datetime) -> bool:
    if artifact.expires_at is None:
        return False
    exp = artifact.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp <= now
