"""Office→PDF conversion for in-chat file preview (类飞书体验).

Browsers render PDF natively but not pptx/docx/xls. This service converts an
artifact's bytes through a Gotenberg server (docker ``gotenberg/gotenberg:8``,
deployed on a separate host) and stores the result as a DERIVED artifact
(``source="preview"``, linked via the origin's generator metadata). Repeat
previews of the same origin hit the cached derived row — LibreOffice runs
once per file, not once per click.

Failure semantics: preview is best-effort. Any error raises ``PreviewUnavailable``
which the API layer maps to a 404-ish response so the frontend falls back to
the download-only panel.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.db import AsyncSessionLocal
from app.models import Artifact

logger = logging.getLogger(__name__)

# Extensions Gotenberg's LibreOffice route can meaningfully convert.
CONVERTIBLE_EXTENSIONS = {".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xls", ".odt", ".ods", ".odp"}


class PreviewUnavailable(Exception):
    """Raised when the preview PDF cannot be produced (service down, bad file)."""


def is_convertible(filename: str | None, media_type: str | None) -> bool:
    """True when this artifact is an Office doc worth converting."""
    if not get_settings().GOTENBERG_URL:
        return False
    name = (filename or "").lower()
    return any(name.endswith(ext) for ext in CONVERTIBLE_EXTENSIONS)


async def _find_cached(db: AsyncSession, origin_id) -> Artifact | None:
    """A previously converted preview artifact for this origin, if any."""
    result = await db.execute(
        select(Artifact)
        .where(Artifact.source == "preview")
        .order_by(Artifact.created_at.desc())
        .limit(200)
    )
    for row in result.scalars():
        gen = row.generator or {}
        if gen.get("preview_of") == str(origin_id):
            return row
    return None


async def _convert_via_gotenberg(data: bytes, filename: str) -> bytes:
    """One LibreOffice round-trip: POST multipart → PDF bytes."""
    cfg = get_settings()
    if not cfg.GOTENBERG_URL:
        raise PreviewUnavailable("preview conversion not configured")
    # Gotenberg's LibreOffice route: POST /forms/libreoffice/convert with the
    # file as multipart; filenames must be latin-1-safe in the upload part.
    safe = filename.encode("latin-1", "replace").decode("latin-1")
    files = {"files": (safe, data)}
    try:
        async with httpx.AsyncClient(timeout=cfg.GOTENBERG_TIMEOUT_S) as client:
            resp = await client.post(
                f"{cfg.GOTENBERG_URL.rstrip('/')}/forms/libreoffice/convert",
                files=files,
            )
    except httpx.HTTPError as exc:
        raise PreviewUnavailable(f"conversion service unreachable: {exc}") from exc
    if resp.status_code != 200 or not resp.content:
        raise PreviewUnavailable(
            f"conversion failed: HTTP {resp.status_code}, {len(resp.content)} bytes"
        )
    return resp.content


async def get_preview_pdf(origin: Artifact) -> Artifact:
    """Return a PDF artifact previewing ``origin`` (cached when possible).

    Looks up an existing derived row first; otherwise converts and persists.
    The derived row is owned by the same user so the normal tenant-scoped
    download endpoint streams it with the same auth path.
    """
    from app.artifacts.service import ArtifactService

    async with AsyncSessionLocal() as db:
        cached = await _find_cached(db, origin.id)
        if cached is not None:
            return cached
        pdf_bytes = await _convert_via_gotenberg(
            _read_artifact_bytes(origin), origin.filename or "document"
        )
        svc = ArtifactService(db)
        preview = await svc.create_from_bytes(
            owner_id=origin.owner_id,
            data=pdf_bytes,
            media_type="application/pdf",
            filename=f"{origin.filename or 'document'}.pdf",
            source="preview",
            generator={"origin": "gotenberg", "preview_of": str(origin.id)},
        )
        return preview


def _read_artifact_bytes(origin: Artifact) -> bytes:
    """Synchronously read the origin artifact's bytes from local storage."""
    from app.core.storage import get_storage

    storage = get_storage()
    with storage.open(origin.storage_key) as fh:
        return fh.read()


    """Frontend-facing endpoint path for an artifact's converted PDF."""
    return f"/api/artifacts/{artifact_id}/preview"
