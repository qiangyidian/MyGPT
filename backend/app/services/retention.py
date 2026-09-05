"""Data-retention sweepers: audit log TTL, terminal run-event pruning, orphan
upload cleanup.

Data previously grew without bound — messages, run_events and audit_events had
no retention policy and files orphaned by failed commits were never reclaimed.
Each sweep is bounded, best-effort, and runs inside the same periodic loop as
the stale-job sweeper (API lifespan) and/or the recovery process.

Config (all optional, all defaulted on):
  * ``AUDIT_RETENTION_DAYS``      — audit_events older than this are deleted.
  * ``RUN_EVENT_RETENTION_DAYS``  — run_events of TERMINAL runs older than this
                                    are deleted (non-terminal runs keep their
                                    event log — recovery replays it).
  * ``ORPHAN_SWEEP_ENABLED``      — scan the local upload dir for files no
                                    attachment/artifact row references.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from app.core.config import get_settings

logger = logging.getLogger(__name__)


async def prune_audit_events(session_factory: Any, days: int | None = None) -> int:
    """Delete audit events older than the retention window (default 365d)."""
    settings = get_settings()
    keep_days = int(days if days is not None else getattr(settings, "AUDIT_RETENTION_DAYS", 365))
    if keep_days <= 0:
        return 0
    from app.models.audit_event import AuditEvent

    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    async with session_factory() as db:
        result = await db.execute(delete(AuditEvent).where(AuditEvent.created_at < cutoff))
        await db.commit()
        deleted = int(result.rowcount or 0)
    if deleted:
        logger.info("retention: pruned %d audit event(s) older than %dd", deleted, keep_days)
    return deleted


async def prune_terminal_run_events(session_factory: Any, days: int | None = None) -> int:
    """Delete run_events belonging to TERMINAL runs past the retention window.

    Terminal runs are never replayed/retried again, so their event logs are
    pure growth. Non-terminal (pending/running) runs are never touched — the
    recovery scheduler needs their events for retries and SSE replay.
    """
    settings = get_settings()
    keep_days = int(days if days is not None else getattr(settings, "RUN_EVENT_RETENTION_DAYS", 90))
    if keep_days <= 0:
        return 0
    from app.models.agent_run import AgentRun
    from app.models.run_event import RunEvent

    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    async with session_factory() as db:
        terminal_ids = (
            await db.execute(
                select(AgentRun.id).where(
                    AgentRun.status.in_(["completed", "failed", "cancelled"]),
                    AgentRun.updated_at < cutoff,
                )
            )
        ).scalars().all()
        if not terminal_ids:
            return 0
        result = await db.execute(delete(RunEvent).where(RunEvent.run_id.in_(terminal_ids)))
        await db.commit()
        deleted = int(result.rowcount or 0)
    if deleted:
        logger.info("retention: pruned %d run event(s) from %d terminal run(s)", deleted, len(terminal_ids))
    return deleted


async def sweep_orphan_uploads(session_factory: Any, *, max_files: int = 500) -> int:
    """Delete files in the local upload dir that no DB row references.

    Orphans come from failed commits between file-save and row-insert. Only
    meaningful for LocalStorage (object stores manage their own lifecycle);
    files younger than 24h are skipped so an in-flight upload is never deleted.
    """
    settings = get_settings()
    if not getattr(settings, "ORPHAN_SWEEP_ENABLED", True):
        return 0
    storage_dir = Path(str(getattr(settings, "STORAGE_DIR", "./data/uploads"))).resolve()
    if not storage_dir.is_dir():
        return 0

    async with session_factory() as db:
        attachment_keys = set(
            (await db.execute(select(_Attachment.storage_key))).scalars().all()
        )
        artifact_keys = set(
            (await db.execute(select(_Artifact.storage_key))).scalars().all()
        )

    referenced = attachment_keys | artifact_keys
    import time as _time

    now = _time.time()
    removed = 0
    for path in storage_dir.rglob("*"):
        if removed >= max_files:
            break
        if not path.is_file():
            continue
        # Local storage keys are stored relative to base dir; match on the
        # tail so either absolute or relative references compare cleanly.
        rel = str(path.relative_to(storage_dir))
        if rel in referenced:
            continue
        # Skip brand-new files (an upload may be mid-commit).
        try:
            if now - path.stat().st_mtime < 24 * 3600:
                continue
        except OSError:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        logger.warning("retention: removed %d orphaned upload file(s)", removed)
    return removed


from app.models.artifact import Artifact as _Artifact
from app.models.chat_attachment import ChatAttachment as _Attachment


class RetentionSweeper:
    """Periodic retention pass; safe to run in the API process or recovery."""

    def __init__(self, session_factory: Any, interval_seconds: int = 6 * 3600) -> None:
        self._session_factory = session_factory
        self._interval = max(int(interval_seconds), 600)
        self._task = None
        self._stop = None

    def start(self) -> None:
        import asyncio

        if self._task is None:
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        import asyncio

        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        import asyncio

        assert self._stop is not None
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("retention sweep failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass

    async def run_once(self) -> None:
        await prune_audit_events(self._session_factory)
        await prune_terminal_run_events(self._session_factory)
        await sweep_orphan_uploads(self._session_factory)
