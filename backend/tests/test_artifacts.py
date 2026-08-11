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
    """A second tenant that owns nothing in this test."""
    from app.models import User

    other = User(
        email="other-art@example.com",
        username="other-art",
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
