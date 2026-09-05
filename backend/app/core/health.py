"""Readiness/liveness health checks for the real dependencies.

The old ``GET /health`` returned ``{"status": "ok"}`` unconditionally — a fake
probe that reported "ready" even when the DB / Redis / Qdrant were down. This
module pings each real dependency (short timeouts, never blocks) so the probe is
meaningful for load-balancer / k8s readiness and for ops dashboards.

Task 11 extends this to a STRICT readiness probe used by ``GET /ready``:

  * ``db``           — DB reachable;
  * ``db_migration`` — the DB's alembic revision is compatible with the repo
                       migration head: at head → ok; behind head (a migration
                       was skipped / stale image) → FAIL; ahead of head
                       (rolled-back code on an upgraded DB, the expand-
                       contract rollback path) → ok; unknown revision → FAIL;
  * ``redis``        — Redis reachable (concurrent quota counters, refresh-token
                       revocation, durable run queue);
  * ``qdrant``       — Qdrant reachable AND the client/server pair is a known-
                       compatible version combo (the repo carries a known
                       client/server version-skew warning);
  * ``storage``      — the upload directory is writable;
  * ``runner``       — a code-execution runner is available (local subprocess or
                       docker);
  * ``chat_model``   — at least one eligible (non-embedding) chat model is
                       configured so a /chat turn won't 400 with no_model_configured.

``GET /health`` stays lenient (liveness: ok when the DB — the hard dependency —
is up); ``GET /ready`` is strict (200 only when ALL components pass, 503 with a
structured body otherwise). Boot does NOT require readiness — the app starts
even if a dependency is down; readiness is the LB/k8s signal.
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import os
import re
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Each probe gets its own short timeout so one slow dependency can't make the
# health endpoint itself unresponsive.
_PROBE_TIMEOUT = 2.0

# The repo's alembic versions directory (backend/migrations/versions), resolved
# relative to this file so it works from the repo, the venv and the Docker image.
_MIGRATIONS_VERSIONS_DIR = (
    Path(__file__).resolve().parents[2] / "migrations" / "versions"
)

_REV_ASSIGN_RE = re.compile(r"^revision(?::\s*[^=\n]+)?\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_DOWN_ASSIGN_RE = re.compile(r"^down_revision(?::\s*[^=\n]+)?\s*=\s*(.+)$", re.MULTILINE)


@lru_cache(maxsize=1)
def _migration_graph() -> dict[str, tuple[str, ...]]:
    """Parse the versions directory into ``{revision: (down_revisions...)}``.

    Mirrors alembic's graph so readiness can classify the DB's revision
    relative to the repo head without importing alembic at app runtime.
    """
    graph: dict[str, tuple[str, ...]] = {}
    for path in sorted(_MIGRATIONS_VERSIONS_DIR.glob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        rev_match = _REV_ASSIGN_RE.search(source)
        if not rev_match:
            continue
        revision = rev_match.group(1)
        downs: tuple[str, ...] = ()
        down_match = _DOWN_ASSIGN_RE.search(source)
        if down_match:
            downs = tuple(re.findall(r"['\"]([^'\"]+)['\"]", down_match.group(1)))
        graph[revision] = downs
    if not graph:
        raise RuntimeError(
            f"no alembic revisions found in {_MIGRATIONS_VERSIONS_DIR}"
        )
    return graph


@lru_cache(maxsize=1)
def _resolve_migration_head() -> str:
    """Resolve the repo's alembic head revision from the versions directory.

    Computed dynamically instead of a hand-maintained constant: a hardcoded
    head drifted from reality once already (health compared against
    ``0010_artifacts`` while ``0011`` was the real head), which failed
    ``/ready`` for every deployment that shipped a migration. The resolver
    walks each migration file's ``revision`` / ``down_revision`` assignments
    (mirroring alembic's graph) so adding a migration requires no edits here.
    """
    graph = _migration_graph()
    referenced = {d for downs in graph.values() for d in downs}
    heads = sorted(r for r in graph if r not in referenced)
    if not heads:
        raise RuntimeError(
            "cannot resolve alembic head: no unreferenced revision found in "
            f"{_MIGRATIONS_VERSIONS_DIR}"
        )
    if len(heads) > 1:
        raise RuntimeError(
            f"multiple alembic heads detected: {heads} — the migration chain "
            "has branched; merge the heads before deploying"
        )
    return heads[0]


# The repo's alembic head. Readiness asserts the DB's current revision == this.
# Resolved from the versions directory at import (see _resolve_migration_head);
# a resolution failure is a hard boot error because alembic-based deploys would
# be equally broken.
REPO_MIGRATION_HEAD: str = _resolve_migration_head()


def classify_db_revision(current: str, head: str, graph: dict[str, tuple[str, ...]]) -> str:
    """Classify the DB's revision relative to the repo head (pure, testable).

    Returns one of:
      * ``"head"``     — DB is exactly at the repo head (normal post-deploy);
      * ``"behind"``   — DB revision is an ancestor of head (a migration was
                         skipped — the stale-image case this gate exists to
                         catch; an ``alembic upgrade head`` is required);
      * ``"ahead"``    — DB revision is known to the repo but NOT an ancestor
                         of head, i.e. the DB has migrated *beyond* this
                         checkout. This is the code-rollback-on-an-upgraded-DB
                         case (expand-contract); it must NOT fail readiness or
                         every rollback would be unhealthy;
      * ``"unknown"``  — revision absent from the repo chain (the migration
                         was deleted, or the DB was built elsewhere).
    """
    if current == head:
        return "head"
    if current not in graph:
        return "unknown"
    # Walk head's ancestry via down_revision links.
    seen: set[str] = set()
    stack = [head]
    while stack:
        rev = stack.pop()
        if rev in seen:
            continue
        seen.add(rev)
        stack.extend(graph.get(rev, ()))
    return "behind" if current in seen else "ahead"

# Qdrant client/server compatibility pairs. The server is pinned to
# qdrant/qdrant:v1.12.x and the client to 1.12.x/1.13.x (see requirements.txt);
# a server older than 1.10 is known to break collection ops against a >=1.12
# client (the repo's documented version-skew warning).
_QDRANT_MIN_SERVER = (1, 10, 0)
# Max supported client/server minor-version skew. A client up to one minor
# ahead/behind the server is supported; a larger gap is a version-skew failure
# (the original warning was client 1.18 vs server 1.12 — a 6-minor gap).
_QDRANT_MAX_SKEW = 1


def _installed_qdrant_client_version() -> tuple[int, ...]:
    """Installed qdrant-client version (empty tuple if metadata unavailable)."""
    try:
        return _parse_version(importlib.metadata.version("qdrant-client"))
    except Exception:  # noqa: BLE001 — best-effort metadata lookup
        return ()


def _ok(reason: str = "ok") -> dict[str, Any]:
    return {"ok": True, "reason": reason}


def _fail(reason: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason}


async def _check_db() -> bool:
    try:
        from sqlalchemy import text

        from app.db import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await asyncio.wait_for(db.execute(text("SELECT 1")), timeout=_PROBE_TIMEOUT)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("health: DB check failed: %s", exc)
        return False


async def _check_db_migration() -> dict[str, Any]:
    """Assert the DB's alembic revision is compatible with the repo head.

    Semantics (see :func:`classify_db_revision`):

      * at head          → ok;
      * behind head      → FAIL (a migration was skipped — stale image);
      * ahead of head    → ok (rolled-back code on an upgraded DB — must stay
                           healthy or every rollback would 503);
      * unknown revision → FAIL.

    Falls back to fail with a clear reason when the DB is the test/dev
    in-memory store (no ``alembic_version`` table — built via ``create_all``).
    """
    try:
        from sqlalchemy import text

        from app.db import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            try:
                row = await asyncio.wait_for(
                    db.execute(text("SELECT version_num FROM alembic_version")),
                    timeout=_PROBE_TIMEOUT,
                )
                current = row.scalar_one_or_none()
            except Exception:  # noqa: BLE001 — table missing etc.
                # Dev/test DBs built via create_all have no alembic_version.
                return _fail(
                    "alembic_version table not present "
                    "(db built via create_all; migration head not tracked)"
                )
            if current is None:
                return _fail("alembic_version row is empty")
            current = str(current)
            relation = classify_db_revision(
                current, REPO_MIGRATION_HEAD, _migration_graph()
            )
            if relation == "head":
                return _ok(f"head={current}")
            if relation == "behind":
                return _fail(
                    f"migration head mismatch: db={current!r} is behind repo "
                    f"head={REPO_MIGRATION_HEAD!r} (run alembic upgrade head)"
                )
            if relation == "ahead":
                return _ok(
                    f"db={current} is ahead of repo head={REPO_MIGRATION_HEAD!r} "
                    "(rolled-back code on an upgraded DB; allowed under "
                    "expand-contract)"
                )
            return _fail(
                f"migration head mismatch: db={current!r} is not a revision in "
                f"this repo's migration chain (repo head={REPO_MIGRATION_HEAD!r})"
            )
    except Exception as exc:  # noqa: BLE001
        return _fail(f"migration check error: {exc}")


async def _check_redis() -> dict[str, Any]:
    try:
        from app.core.redis import get_redis

        client = get_redis()
        await asyncio.wait_for(client.ping(), timeout=_PROBE_TIMEOUT)
        return _ok("reachable")
    except Exception as exc:  # noqa: BLE001
        # Redis is optional at boot (refresh revocation / rate limiting /
        # quota counters all degrade gracefully) but it IS required for /ready.
        return _fail(f"unreachable: {exc}")


def _parse_version(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(v).split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


async def _check_qdrant() -> dict[str, Any]:
    """Qdrant reachable + client/server version pair compatible."""
    try:
        from app.rag.qdrant_store import get_vector_store

        store = get_vector_store()
        collections = await asyncio.wait_for(
            store._client.get_collections(), timeout=_PROBE_TIMEOUT
        )
        if collections is None:
            return _fail("no collections response")
        # Probe the server version (compat gate). The AsyncQdrantClient.info()
        # call returns a VersionInfo with the server's title/version.
        try:
            info = await asyncio.wait_for(store._client.info(), timeout=_PROBE_TIMEOUT)
            server_version = getattr(info, "version", "") or ""
            sv = _parse_version(str(server_version))
            if sv and sv[:3] < _QDRANT_MIN_SERVER:
                return _fail(
                    f"qdrant server {server_version} older than supported "
                    f"{'.'.join(map(str, _QDRANT_MIN_SERVER))} (client/server skew)"
                )
            # Guard the client/server minor-version skew so a drifted client
            # can't pass readiness silently even if the requirements pin is
            # bypassed (the repo's known skew was client 1.18 vs server 1.12).
            client_ver = _installed_qdrant_client_version()
            client_label = ".".join(map(str, client_ver)) if client_ver else "unknown"
            if client_ver and sv:
                client_minor = client_ver[1] if len(client_ver) > 1 else 0
                server_minor = sv[1] if len(sv) > 1 else 0
                if abs(client_minor - server_minor) > _QDRANT_MAX_SKEW:
                    return _fail(
                        f"client/server version skew: client={client_label} "
                        f"server={server_version} (max supported minor skew "
                        f"{_QDRANT_MAX_SKEW}; pin qdrant-client to match the server)"
                    )
            return _ok(f"reachable; server={server_version}; client={client_label}")
        except Exception as inner:  # noqa: BLE001 — version probe failed
            # Reachable, but the compatibility probe could not run. A reachable-
            # but-unprobed Qdrant is treated as NOT ready: the operator must see
            # that the client/server pair couldn't be validated (the repo carries
            # a known version-skew warning), rather than a silent "ok".
            return _fail(f"reachable but version probe failed: {inner}")
    except Exception as exc:  # noqa: BLE001
        return _fail(f"unreachable: {exc}")


async def _check_storage() -> dict[str, Any]:
    """The configured upload directory must be writable."""
    try:
        settings = get_settings()
        storage_dir = os.path.abspath(str(getattr(settings, "STORAGE_DIR", "./data/uploads")))
        os.makedirs(storage_dir, exist_ok=True)
        # Write + remove a probe file to confirm writability.
        fd, path = tempfile.mkstemp(prefix=".ready_probe_", dir=storage_dir)
        os.write(fd, b"ok")
        os.close(fd)
        os.unlink(path)
        return _ok(f"writable: {storage_dir}")
    except Exception as exc:  # noqa: BLE001
        return _fail(f"not writable: {exc}")


async def _check_runner() -> dict[str, Any]:
    """A code-execution runner is available (local subprocess or docker)."""
    try:
        settings = get_settings()
        mode = str(getattr(settings, "SANDBOX_MODE", "local")).lower()
        if mode == "docker":
            # Confirm the docker binary is on PATH.
            proc = await asyncio.create_subprocess_exec(
                "docker", "--version",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=_PROBE_TIMEOUT)
            if proc.returncode == 0:
                return _ok("docker runner available")
            return _fail("docker --version exited non-zero")
        # local mode: the subprocess runner is always available.
        return _ok("local runner available")
    except Exception as exc:  # noqa: BLE001
        return _fail(f"runner check error: {exc}")


async def _check_chat_model() -> dict[str, Any]:
    """At least one eligible (non-embedding) chat ModelConfig must exist."""
    try:
        from sqlalchemy import func, select

        from app.db import AsyncSessionLocal
        from app.models.model_config import ModelConfig

        async with AsyncSessionLocal() as db:
            count = await asyncio.wait_for(
                db.execute(
                    select(func.count())
                    .select_from(ModelConfig)
                    .where(ModelConfig.is_embedding.is_(False))
                ),
                timeout=_PROBE_TIMEOUT,
            )
            n = int(count.scalar_one())
            if n > 0:
                return _ok(f"{n} chat model(s) configured")
            return _fail("no eligible chat model configured")
    except Exception as exc:  # noqa: BLE001
        return _fail(f"chat model check error: {exc}")


async def check_health() -> dict[str, Any]:
    """Lenient liveness probe: ok when the DB (hard dependency) is up.

    Redis/Qdrant are reported but optional (the app degrades without them).
    Used by ``GET /health``.
    """
    db_ok = await _check_db()
    redis_info = await _check_redis()
    qdrant_info = await _check_qdrant()
    components = {
        "db": db_ok,
        "redis": redis_info["ok"],
        "qdrant": qdrant_info["ok"],
    }
    return {
        "status": "ok" if db_ok else "degraded",
        "components": components,
    }


async def check_readiness() -> dict[str, Any]:
    """Strict readiness probe: ok only when ALL components pass.

    Returns a structured per-component body (each entry has ``ok`` + ``reason``).
    Used by ``GET /ready`` (200 on full pass, 503 otherwise). Boot does not
    require readiness — this is the LB/k8s signal.
    """
    db_ok, migration, redis, qdrant, storage, runner, chat_model = (
        await asyncio.gather(
            _check_db(),
            _check_db_migration(),
            _check_redis(),
            _check_qdrant(),
            _check_storage(),
            _check_runner(),
            _check_chat_model(),
        )
    )
    components = {
        "db": _ok() if db_ok else _fail("SELECT 1 failed"),
        "db_migration": migration,
        "redis": redis,
        "qdrant": qdrant,
        "storage": storage,
        "runner": runner,
        "chat_model": chat_model,
    }
    all_ok = all(c["ok"] for c in components.values())
    return {
        "status": "ready" if all_ok else "not_ready",
        "components": components,
    }
