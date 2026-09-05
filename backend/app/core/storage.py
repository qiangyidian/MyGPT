"""Pluggable file storage for uploaded documents.

Only the local filesystem backend exists. Business code obtains a backend via
``get_storage()`` — never constructs paths or talks to the FS directly, so a
future S3/MinIO backend can slot in behind the same ``StorageBackend`` protocol
without touching call sites.
"""
from __future__ import annotations

import os
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import IO

from fastapi import UploadFile

from app.core.config import get_settings


class StorageBackend(ABC):
    """Interface every storage backend implements."""

    @abstractmethod
    async def save(
        self, upload_file: UploadFile, user_id, *, allowed_extensions: set[str] | None = None
    ) -> str:
        """Persist ``upload_file`` for ``user_id``; return the stored path/key.

        ``allowed_extensions`` overrides the configured allow-list (used by chat
        attachments, which accept a broader set than KB uploads, e.g. images).
        """

    @abstractmethod
    def open(self, path: str) -> IO[bytes]:
        """Open the stored object for binary reading."""

    @abstractmethod
    async def delete(self, path: str) -> None:
        """Remove the stored object; ignore if missing."""

    @staticmethod
    def _safe_suffix(filename: str) -> str:
        """Return a lowercased extension including the dot, or '' if none.

        Extension is validated against the allow-list by the caller; here we
        just normalize it and strip any path components a client may inject.
        """
        name = os.path.basename(filename or "")
        _, ext = os.path.splitext(name)
        return ext.lower()


class LocalStorage(StorageBackend):
    """Stores uploads under ``settings.STORAGE_DIR/<user_id>/<uuid><ext>``.

    Filenames are replaced with a UUID to avoid collisions and to prevent path
    traversal (the original name is never trusted as part of the path). The
    upload extension is validated against the configured allow-list.
    """

    def __init__(self, base_dir: str | os.PathLike[str] | None = None) -> None:
        settings = get_settings()
        self.base_dir = Path(base_dir if base_dir is not None else settings.STORAGE_DIR)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def save(
        self, upload_file: UploadFile, user_id, *, allowed_extensions: set[str] | None = None
    ) -> str:
        settings = get_settings()
        ext = self._safe_suffix(upload_file.filename or "")
        allow = allowed_extensions if allowed_extensions is not None else settings.allowed_extensions
        if ext and ext not in allow:
            # Let callers decide policy; here we reject disallowed types up front.
            raise ValueError(f"File type {ext or '(none)'} is not allowed")

        user_dir = self.base_dir / str(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)

        stored_name = f"{uuid.uuid4().hex}{ext}"
        dest = (user_dir / stored_name).resolve()
        # Defence in depth: ensure the resolved target stays under base_dir. Use
        # Path.relative_to (not str.startswith) so a prefix collision like
        # base_dir "uploads" vs a sibling "uploads-x" can't slip through, and
        # path-separator differences are handled correctly.
        base_resolved = self.base_dir.resolve()
        try:
            dest.relative_to(base_resolved)
        except ValueError:
            raise ValueError("Invalid storage path")

        # Stream the upload to disk so large files don't get fully buffered.
        await upload_file.seek(0)
        with open(dest, "wb") as out:
            chunk = await upload_file.read(1024 * 1024)
            while chunk:
                out.write(chunk)
                chunk = await upload_file.read(1024 * 1024)
        return str(dest)

    def open(self, path: str) -> IO[bytes]:
        # Resolve against base and refuse to escape it (relative_to, not startswith).
        target = (self.base_dir / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
        base_resolved = self.base_dir.resolve()
        try:
            target.relative_to(base_resolved)
        except ValueError:
            raise ValueError("Invalid storage path")
        return open(target, "rb")

    async def delete(self, path: str) -> None:
        target = Path(path) if os.path.isabs(path) else (self.base_dir / path)
        if target.exists():
            target.unlink()


_backend: StorageBackend | None = None


def get_storage(backend: str | None = None) -> StorageBackend:
    """Return the configured storage backend (cached).

    ``backend`` overrides ``settings.STORAGE_BACKEND`` for tests; the cached
    instance always reflects the production selection otherwise.
    """
    global _backend
    if _backend is not None and backend is None:
        return _backend

    settings = get_settings()
    chosen = (backend or settings.STORAGE_BACKEND).lower().strip()
    if chosen == "minio":
        raise NotImplementedError(
            "MinIO/S3 storage backend is not implemented; set STORAGE_BACKEND=local"
        )
    instance: StorageBackend = LocalStorage()
    if backend is None:
        _backend = instance
    return instance
