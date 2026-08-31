"""Artifacts router: tenant-scoped upload + authorized streaming download.

The client NEVER sees a local filesystem path or a ``storage_key``: it references
an artifact by its opaque id and fetches bytes through the authenticated
``GET /api/artifacts/{id}`` endpoint. A foreign or unknown id yields 404 so
existence never leaks. A checksum mismatch on read yields 410.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.service import ArtifactService
from app.core.deps import get_current_user
from app.core.exceptions import AppException
from app.db import get_db
from app.models import User

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])

NOT_FOUND = status.HTTP_404_NOT_FOUND


def _envelope_status(exc: AppException) -> HTTPException:
    """Re-raise an AppException as the uniform HTTPException for routers."""
    return HTTPException(exc.status_code, exc.message, headers={"X-Code": exc.code})


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_artifact(
    file: UploadFile = File(...),
    source: str = Form("upload"),
    run_id: uuid.UUID | None = Form(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create an artifact from an upload. Returns the opaque id + metadata.

    ``source`` must be one of ``upload | tool_output | spill | generation``.
    The response carries NO storage path/ key — only the opaque id the client
    uses for the authenticated download endpoint.
    """
    svc = ArtifactService(db)
    try:
        art = await svc.create_from_upload(
            owner_id=user.id,
            upload_file=file,
            source=source,
            run_id=run_id,
        )
    except AppException as exc:
        raise _envelope_status(exc)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return {
        "id": str(art.id),
        "media_type": art.media_type,
        "size": art.size,
        "checksum": art.checksum,
        "filename": art.filename,
        "source": art.source,
        "created_at": art.created_at.isoformat() if art.created_at else None,
    }


@router.get("/{artifact_id}")
async def download_artifact(
    artifact_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Authorized, tenant-scoped **chunked** streaming download.

    Owner/admin only; foreign/unknown id → 404 (no existence leak). The bytes
    are streamed from the storage backend in fixed-size chunks so a large
    artifact is never fully buffered in RAM. Authorization (owner check +
    retention) runs BEFORE the first chunk; a checksum mismatch detected
    mid-stream aborts the response (status/headers are already committed by
    then) and is logged.
    """
    svc = ArtifactService(db)
    try:
        meta = await svc.get(artifact_id, user)
        body = await svc.open_stream(artifact_id, user)
    except AppException as exc:
        raise _envelope_status(exc)
    # HTTP headers are latin-1 — a CJK filename crashes the response with
    # UnicodeEncodeError. RFC 5987: ASCII fallback + filename*=UTF-8''percent-
    # encoded original. Browsers prefer filename*; curl keeps the fallback.
    from urllib.parse import quote

    raw_name = meta.filename or "artifact"
    safe_name = raw_name.replace('"', "").replace("\r", "").replace("\n", "")
    ascii_name = safe_name.encode("latin-1", "replace").decode("latin-1")
    return StreamingResponse(
        body,
        media_type=meta.media_type or "application/octet-stream",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(safe_name)}"
            ),
            "Cache-Control": "private, no-store",
            "Content-Length": str(meta.size),
        },
    )


@router.get("/{artifact_id}/meta")
async def get_artifact_meta(
    artifact_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Lightweight metadata (no bytes) for one artifact — powers the inline
    handle chips' filename/size display without downloading the blob."""
    svc = ArtifactService(db)
    try:
        meta = await svc.get(artifact_id, user)
    except AppException as exc:
        raise _envelope_status(exc)
    return {
        "id": str(meta.id),
        "media_type": meta.media_type,
        "size": meta.size,
        "filename": meta.filename,
        "source": meta.source,
        "created_at": meta.created_at.isoformat() if meta.created_at else None,
    }


@router.get("", response_model=None)
async def list_artifacts(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List the caller's own, non-expired artifacts (newest first)."""
    svc = ArtifactService(db)
    rows = await svc.list_for_owner(user.id)
    return [
        {
            "id": str(r.id),
            "media_type": r.media_type,
            "size": r.size,
            "checksum": r.checksum,
            "filename": r.filename,
            "source": r.source,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.delete("/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_artifact(
    artifact_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    svc = ArtifactService(db)
    try:
        await svc.delete(artifact_id, user)
    except AppException as exc:
        raise _envelope_status(exc)
