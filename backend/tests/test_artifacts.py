"""Task 10: first-class ArtifactService — tenant-scoped authorization,
checksum verification on read, retention enforcement, and opaque storage keys.

The model/client NEVER sees a local filesystem path: only an opaque
``artifact:<id>`` handle. Download authorization is owner-scoped (admin
bypasses); a foreign user gets 404 (no existence leak). Reads verify the
sha256 checksum to detect corruption/tamper. Expired artifacts are hidden.
"""
from __future__ import annotations

import datetime as _dt
import uuid

import pytest
from fastapi import UploadFile
import io

from app.artifacts.service import ArtifactService
from app.core.exceptions import AppException
from app.core.storage import get_storage

SEEDED = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _other_user(db_session) -> uuid.UUID:
    """A second tenant that owns nothing in this test (unique per call)."""
    from app.models import User

    suffix = uuid.uuid4().hex[:8]
    other = User(
        email=f"other-art-{suffix}@example.com",
        username=f"other-art-{suffix}",
        password_hash="x",
        role="user",
        is_active=True,
    )
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)
    return other.id


# ---------------------------------------------------------------------------
# Create path: checksum + size + media_type persisted.
# ---------------------------------------------------------------------------
async def test_create_from_bytes_persists_checksum_size_media_type(db_session):
    svc = ArtifactService(db_session)
    data = b"hello artifact world"
    art = await svc.create_from_bytes(
        owner_id=SEEDED,
        data=data,
        media_type="text/plain",
        filename="hello.txt",
        source="upload",
    )
    import hashlib

    assert art.checksum == hashlib.sha256(data).hexdigest()
    assert art.size == len(data)
    assert art.media_type == "text/plain"
    assert art.owner_id == SEEDED
    assert art.source == "upload"
    # storage_key is set but NEVER handed to the client (only the opaque id).
    assert art.storage_key


async def test_create_from_upload_streams_to_storage(db_session):
    svc = ArtifactService(db_session)
    data = b"\x89PNG\r\n\x1a\n fake png body"
    upload = UploadFile(filename="shot.png", file=io.BytesIO(data))
    art = await svc.create_from_upload(
        owner_id=SEEDED, upload_file=upload, source="generation",
    )
    assert art.size == len(data)
    assert art.media_type == "image/png"
    try:
        assert art.checksum
    finally:
        await get_storage().delete(art.storage_key)


# ---------------------------------------------------------------------------
# Tenant-scoped authorization: foreign user → 404 (no existence leak).
# ---------------------------------------------------------------------------
async def test_open_returns_verified_bytes_for_owner(db_session):
    svc = ArtifactService(db_session)
    data = b"owner-only payload"
    art = await svc.create_from_bytes(
        owner_id=SEEDED, data=data, media_type="text/plain",
        filename="p.txt", source="upload",
    )
    try:
        got = await svc.open(art.id, SEEDED)
        assert got == data
    finally:
        await get_storage().delete(art.storage_key)


async def test_artifact_download_is_tenant_scoped_404_on_foreign(db_session):
    svc = ArtifactService(db_session)
    art = await svc.create_from_bytes(
        owner_id=SEEDED, data=b"secret", media_type="text/plain",
        filename="s.txt", source="upload",
    )
    other_id = await _other_user(db_session)
    foreign_user = _user_with_id(other_id)
    try:
        with pytest.raises(AppException) as exc:
            await svc.open(art.id, foreign_user)
        # 404 (not 403) so a foreign user cannot learn the artifact exists.
        assert exc.value.status_code == 404
    finally:
        await get_storage().delete(art.storage_key)


async def test_open_404_on_unknown_id(db_session):
    svc = ArtifactService(db_session)
    with pytest.raises(AppException) as exc:
        await svc.open(uuid.uuid4(), _user_with_id(SEEDED))
    assert exc.value.status_code == 404


async def test_admin_can_open_foreign_artifact(db_session):
    svc = ArtifactService(db_session)
    art = await svc.create_from_bytes(
        owner_id=SEEDED, data=b"audit me", media_type="text/plain",
        filename="a.txt", source="upload",
    )
    admin = _user_with_id(SEEDED, role="admin")
    try:
        got = await svc.open(art.id, admin)
        assert got == b"audit me"
    finally:
        await get_storage().delete(art.storage_key)


# ---------------------------------------------------------------------------
# Checksum verification on read: corruption/tamper is detected.
# ---------------------------------------------------------------------------
async def test_checksum_verified_on_read_detects_corruption(db_session):
    svc = ArtifactService(db_session)
    art = await svc.create_from_bytes(
        owner_id=SEEDED, data=b"original", media_type="text/plain",
        filename="c.txt", source="upload",
    )
    # Corrupt the underlying stored object directly.
    key = art.storage_key
    try:
        _overwrite_storage(key, b"tampered!!")
        with pytest.raises(AppException) as exc:
            await svc.open(art.id, _user_with_id(SEEDED))
        assert exc.value.code == "artifact_checksum_mismatch"
    finally:
        await get_storage().delete(key)


# ---------------------------------------------------------------------------
# Retention: an expired artifact is hidden from open.
# ---------------------------------------------------------------------------
async def test_retention_expiry_hides_artifact(db_session):
    svc = ArtifactService(db_session)
    art = await svc.create_from_bytes(
        owner_id=SEEDED, data=b"ephemeral", media_type="text/plain",
        filename="e.txt", source="spill",
        expires_at=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=1),
    )
    try:
        with pytest.raises(AppException) as exc:
            await svc.open(art.id, _user_with_id(SEEDED))
        assert exc.value.status_code == 404
    finally:
        await get_storage().delete(art.storage_key)


async def test_retention_not_expired_is_openable(db_session):
    svc = ArtifactService(db_session)
    art = await svc.create_from_bytes(
        owner_id=SEEDED, data=b"alive", media_type="text/plain",
        filename="a.txt", source="spill",
        expires_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1),
    )
    try:
        got = await svc.open(art.id, _user_with_id(SEEDED))
        assert got == b"alive"
    finally:
        await get_storage().delete(art.storage_key)


# ---------------------------------------------------------------------------
# Provenance is recorded so the frontend/audit can attribute the artifact.
# ---------------------------------------------------------------------------
async def test_provenance_records_generator_info(db_session):
    svc = ArtifactService(db_session)
    art = await svc.create_from_bytes(
        owner_id=SEEDED, data=b"gen output", media_type="text/plain",
        filename="out.txt", source="generation",
        generator={"tool": "code_runner", "run_id": "r-1"},
    )
    assert art.generator == {"tool": "code_runner", "run_id": "r-1"}


# ---------------------------------------------------------------------------
# Spill → artifact backing (Task 7 seam backed by the real service).
# ---------------------------------------------------------------------------
async def test_spill_to_artifact_creates_opaque_handle(db_session):
    """A spilled blob becomes a first-class Artifact; the model sees only the
    opaque ``artifact:<id>`` handle, never a raw path."""
    from app.agents.output_spill import spill_to_artifact

    big = "line of content\n" * 2000
    preview, handle = await spill_to_artifact(
        db_session, owner_id=SEEDED, text=big, budget_tokens=100, key="scan",
    )
    assert handle is not None
    assert handle.id.startswith("artifact:")
    # The preview is much smaller than the original.
    assert len(preview) < len(big)
    # The handle resolves back to the stored bytes via the ArtifactService.
    art_id = uuid.UUID(handle.id.split(":", 1)[1])
    svc = ArtifactService(db_session)
    try:
        got = await svc.open(art_id, _user_with_id(SEEDED))
        assert got == big.encode("utf-8")
    finally:
        await get_storage().delete(handle.storage_key)


async def test_spill_to_artifact_returns_none_when_under_budget(db_session):
    from app.agents.output_spill import spill_to_artifact

    preview, handle = await spill_to_artifact(
        db_session, owner_id=SEEDED, text="short", budget_tokens=1000,
    )
    assert handle is None
    assert preview == "short"


# ---------------------------------------------------------------------------
# Generated files (tool outputs, screenshots, ...) → first-class artifact.
# ---------------------------------------------------------------------------
async def test_artifact_for_generated_returns_opaque_id(db_session):
    from app.services import attachment_service
    from types import SimpleNamespace

    user = SimpleNamespace(id=SEEDED, role="user")
    art_id = await attachment_service.artifact_for_generated(
        db_session,
        user=user,
        data=b"\x89PNG\r\n\x1a\n screenshot bytes",
        media_type="image/png",
        filename="screenshot.png",
        source="generation",
        generator={"tool": "screenshot"},
    )
    assert isinstance(art_id, uuid.UUID)
    svc = ArtifactService(db_session)
    try:
        art = await svc.get(art_id, user)
        assert art.source == "generation"
        assert art.media_type == "image/png"
        assert art.generator == {"tool": "screenshot"}
    finally:
        await get_storage().delete(art.storage_key)


# ---------------------------------------------------------------------------
# I1: chunked streaming download — a large artifact streams in chunks (memory
# bounded), NOT buffered whole into RAM.
# ---------------------------------------------------------------------------
async def test_open_stream_yields_chunks_not_whole_buffer(db_session):
    svc = ArtifactService(db_session)
    # Larger than the default 64KB chunk so we get multiple chunks.
    data = b"A" * (200 * 1024)
    art = await svc.create_from_bytes(
        owner_id=SEEDED, data=data, media_type="application/octet-stream",
        filename="big.bin", source="generation",
    )
    try:
        gen = await svc.open_stream(art.id, _user_with_id(SEEDED), chunk_size=64 * 1024)
        chunks = list(gen)
        # Multiple chunks ⇒ the file was NOT buffered whole.
        assert len(chunks) > 1
        assert b"".join(chunks) == data
        # Each chunk (except possibly the last) is bounded to chunk_size.
        assert all(len(c) <= 64 * 1024 for c in chunks)
    finally:
        await get_storage().delete(art.storage_key)


async def test_open_stream_tenant_scoped_404_on_foreign(db_session):
    svc = ArtifactService(db_session)
    art = await svc.create_from_bytes(
        owner_id=SEEDED, data=b"private stream", media_type="text/plain",
        filename="p.txt", source="upload",
    )
    other_id = await _other_user(db_session)
    try:
        with pytest.raises(AppException) as exc:
            await svc.open_stream(art.id, _user_with_id(other_id))
        assert exc.value.status_code == 404
    finally:
        await get_storage().delete(art.storage_key)


# ---------------------------------------------------------------------------
# I2: production spill wiring — ContextManager.spill_tool_result persists a
# real, tenant-scoped Artifact whose opaque handle resolves to a downloadable
# row (owner 200 via API, foreign 404). This is the dead-code fix.
# ---------------------------------------------------------------------------
async def test_production_spill_wiring_creates_downloadable_artifact(client, db_session):
    """End-to-end: an oversized tool result spilled through the production
    ContextManager becomes a real Artifact; GET /api/artifacts/{id} downloads
    it for the owner and 404s for a foreign user."""
    from app.agents.context_manager import ContextManager
    from app.agents.output_spill import production_spill_writer
    from app.artifacts.context import (
        reset_artifact_spill_context,
        set_artifact_spill_context,
    )
    from tests.conftest import TestSessionLocal, auth_headers, get_access_token

    big = "tool output line\n" * 4000  # well over a small spill budget
    # Bind the artifact auth context to the seeded user + the test session
    # factory (same engine the API client under test reads from).
    token = set_artifact_spill_context(
        owner_id=SEEDED, db_factory=TestSessionLocal, run_id=None,
    )
    try:
        mgr = ContextManager(
            summarize_fn=lambda older: "",
            spill_writer=production_spill_writer,
        )
        # spill_tool_result is sync; its writer spawns a worker thread that
        # persists the blob as a real Artifact via the bound db_factory.
        preview, handle = mgr.spill_tool_result(big, budget_tokens=100, key="scan")
    finally:
        reset_artifact_spill_context(token)

    assert handle is not None
    # The handle id is the REAL artifact row id (not the placeholder uuid the
    # pure spill seam mints before the writer runs).
    assert handle.id.startswith("artifact:")
    art_uuid = uuid.UUID(handle.id.split(":", 1)[1])
    try:
        # The artifact row exists + is owned by the seeded user.
        svc = ArtifactService(db_session)
        art = await svc._get_owned(art_uuid, _user_with_id(SEEDED))
        assert art.owner_id == SEEDED
        assert art.source == "spill"

        # GET /api/artifacts/{id} downloads it for the owner (200, full bytes).
        resp = await client.get(
            f"/api/artifacts/{art_uuid}", headers=auth_headers(get_access_token())
        )
        assert resp.status_code == 200
        assert resp.content == big.encode("utf-8")

        # Foreign user → 404 (no existence leak).
        other_id = await _other_user(db_session)
        other_token = get_access_token(str(other_id))
        resp2 = await client.get(
            f"/api/artifacts/{art_uuid}", headers=auth_headers(other_token)
        )
        assert resp2.status_code == 404
    finally:
        await get_storage().delete(handle.storage_key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _user_with_id(user_id, *, role: str = "user"):
    from types import SimpleNamespace

    return SimpleNamespace(id=user_id, role=role)


def _overwrite_storage(key: str, data: bytes) -> None:
    """Overwrite the stored object at ``key`` (simulates corruption/tamper)."""
    import os

    # LocalStorage writes a real file at the absolute key path.
    if os.path.isabs(key) and os.path.exists(key):
        with open(key, "wb") as fh:
            fh.write(data)
        return
    raise RuntimeError(f"cannot corrupt non-local storage key {key!r}")
