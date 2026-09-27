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


def _resolve_key(base: Path, value: str) -> str:
    """Normalize a stored reference to a comparable absolute path.

    ``LocalStorage.save()`` returns an absolute path, so every one of
    ``ChatAttachment.storage_key`` / ``Artifact.storage_key`` /
    ``Document.file_path`` holds absolute strings; a path relative to the base
    dir is only ever compared against those if it is expanded first. Slashes are
    unified because SQLite/Python mix ``\\`` and ``/`` across platforms.
    """
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    try:
        path = path.resolve()
    except OSError:
        pass
    return str(path).replace("\\", "/")


async def sweep_orphan_uploads(session_factory: Any, *, max_files: int = 500) -> int:
    """Delete files in the local upload dir that no DB row references.

    Orphans come from failed commits between file-save and row-insert. Only
    meaningful for LocalStorage (object stores manage their own lifecycle);
    files younger than 24h are skipped so an in-flight upload is never deleted.

    The reference set must cover **every** table that owns a file under
    ``STORAGE_DIR`` — attachments, artifacts, and knowledge-base documents.
    A missing source turns this sweep into silent bulk deletion of live files.
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
        document_keys = set((await db.execute(select(_Document.file_path))).scalars().all())

    referenced = {
        _resolve_key(storage_dir, key)
        for key in (attachment_keys | artifact_keys | document_keys)
        if key
    }
    if not referenced:
        # Nothing claims a file, which also means we cannot tell live uploads
        # from orphans (fresh install, or the lookup returned nothing useful).
        # Mass-deletion is the unrecoverable failure mode here, so skip.
        logger.warning("retention: empty reference set, skipping orphan sweep")
        return 0
    import time as _time

    now = _time.time()
    removed = 0
    for path in storage_dir.rglob("*"):
        if removed >= max_files:
            break
        if not path.is_file():
            continue
        if _resolve_key(storage_dir, str(path)) in referenced:
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
from app.models.document import Document as _Document
from app.models.knowledge_base import KnowledgeBase as _KnowledgeBase

# KB 向量 collection 的名字由 KB id 派生（``kb_`` + 32 位十六进制，见
# app.rag.rag_service.collection_name）。孤儿回收**只**认这个精确形状 ——
# 共享的 chat_attachments、记忆 collection 等一律不碰。
_KB_COLLECTION_RE = None


def _kb_collection_pattern():
    global _KB_COLLECTION_RE
    if _KB_COLLECTION_RE is None:
        import re

        _KB_COLLECTION_RE = re.compile(r"^kb_[0-9a-f]{32}$")
    return _KB_COLLECTION_RE


def _kb_hex(collection: str) -> str | None:
    """kb_<32hex> -> 那 32 位十六进制；形状不符返回 None（绝不拿去比对）。"""
    if not _kb_collection_pattern().match(collection):
        return None
    return collection[3:]


async def sweep_orphan_collections(
    session_factory: Any, *, vector_store: Any = None, max_drops: int = 50
) -> int:
    """删掉没有对应知识库行的 ``kb_*`` collection（Qdrant 侧的存储泄漏）。

    删除知识库时先落库、后删向量；向量那一步是 best-effort（Qdrant 抖动不能
    把用户的删除变成 502），所以每次失败都会留下一个**再也指不回来**的孤儿
    collection —— 名字派生自已删除的 KB id，正常代码路径永远不会再触碰它。
    这里就是那条兜底路径。

    安全边界：只处理精确匹配 ``kb_<32hex>`` 的名字；KB 行存在 = 在用；读不到 KB
    集合（查询失败/空表）时整轮跳过，因为「无法区分在用与孤儿」时误删是不可逆的。
    """
    if not getattr(get_settings(), "ORPHAN_COLLECTION_SWEEP_ENABLED", True):
        return 0
    if vector_store is None:
        try:
            from app.rag.qdrant_store import get_vector_store

            store = get_vector_store()
        except Exception:
            return 0
    else:
        store = vector_store

    try:
        collections = await store.list_collections()
    except Exception as exc:
        logger.debug("retention: qdrant collection listing failed: %s", exc)
        return 0
    candidates = [c for c in collections if _kb_hex(c) is not None]
    if not candidates:
        return 0

    try:
        async with session_factory() as db:
            live = {
                # SQLite 的 UUID 类型适配不如 PG 稳定，两边都可能出现 str 或
                # uuid.UUID —— 一律按字符串比对，避免「明明在用却被当成孤儿」。
                str(row).replace("-", "")
                for row in (
                    await db.execute(select(_KnowledgeBase.id))
                ).scalars().all()
            }
    except Exception:
        # 无法判定谁还在用 —— 跳过，绝不猜。
        logger.warning("retention: KB lookup failed, skipping collection sweep")
        return 0
    if not live:
        # 一条知识库都读不到，就无法区分「平台是空的」与「查询出错了」；后者的
        # 代价是清空所有人的向量，前者只是少回收一次。所以一律跳过。
        logger.warning(
            "retention: knowledge-base lookup returned nothing, skipping collection sweep"
        )
        return 0

    dropped = 0
    for collection in candidates:
        if dropped >= max_drops:
            break
        if _kb_hex(collection) in live:
            continue
        try:
            await store.drop_collection(collection)
            dropped += 1
        except Exception:
            logger.debug("retention: drop collection %s failed", collection, exc_info=True)
    if dropped:
        logger.warning("retention: dropped %d orphaned Qdrant collection(s)", dropped)
    return dropped


class RetentionSweeper:
    """Periodic retention pass; safe to run in the API process or recovery.

    Leader-gated by default (finding 39): every API replica plus the worker used
    to start one of these, so a 3-replica deploy ran the same DELETE-heavy pass
    three times concurrently — redundant work that also deadlocks the prune
    queries against each other on Postgres. With ``leader_name`` set, only the
    holder of the Postgres advisory lock sweeps; a peer takes over within one
    heartbeat if the leader dies, so gating costs availability, not coverage.
    Pass ``leader_name=None`` to force unconditional running (single-process
    dev, or a test that wants the pass to happen now).
    """

    def __init__(
        self,
        session_factory: Any,
        interval_seconds: int = 6 * 3600,
        *,
        leader_name: str | None = "retention",
    ) -> None:
        self._session_factory = session_factory
        self._interval = max(int(interval_seconds), 600)
        self._task = None
        self._stop = None
        self._gate = None
        if leader_name is not None:
            from app.core.leader import LeaderGate

            self._gate = LeaderGate(leader_name, session_factory=session_factory)
        self._leader_name = leader_name

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
        if self._gate is not None:
            await self._gate.release()

    async def _loop(self) -> None:
        import asyncio

        assert self._stop is not None
        while not self._stop.is_set():
            try:
                may_run = True if self._gate is None else await self._gate.acquire()
                if may_run:
                    await self.run_once()
                    self._beat()
            except Exception:
                logger.exception("retention sweep failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass

    def _beat(self) -> None:
        """Record progress for the container health probe (best-effort)."""
        if self._leader_name is None:
            return
        try:
            from app.healthcheck import beat

            beat(self._leader_name, detail={"interval_seconds": self._interval})
        except Exception:  # pragma: no cover - a probe file must never break a sweep
            logger.debug("retention heartbeat failed", exc_info=True)

    async def run_once(self) -> None:
        await prune_audit_events(self._session_factory)
        await prune_terminal_run_events(self._session_factory)
        await sweep_orphan_uploads(self._session_factory)
        await sweep_orphan_collections(self._session_factory)
        await self._release_holds()

    async def _release_holds(self) -> None:
        """退还被杀进程留下的悬挂轮次预留（finding 40）。

        准入预留是"这轮会跑完并结算"的承诺；OOMKill 之后没人兑现，用户的余额
        就长期被压低。这条只在 leader 锁下运行，所以两个进程不会抢同一批 hold。
        与其余 sweep 一样 best-effort：失败只影响一批预留，不该带走整个 sweep。
        """
        from app.services.credit_service import release_stale_turn_holds

        try:
            async with self._session_factory() as session:
                n = await release_stale_turn_holds(session)
                await session.commit()
            if n:
                logger.info("retention: released %d stale turn hold(s)", n)
        except Exception:
            logger.warning("retention: stale turn-hold release failed", exc_info=True)
