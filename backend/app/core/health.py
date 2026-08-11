"""Readiness/liveness health checks for the real dependencies.

The old ``GET /health`` returned ``{"status": "ok"}`` unconditionally — a fake
probe that reported "ready" even when the DB / Redis / Qdrant were down. This
module pings each real dependency (short timeouts, never blocks) so the probe is
meaningful for load-balancer / k8s readiness and for ops dashboards.

Task 11 extends this to a STRICT readiness probe used by ``GET /ready``:

  * ``db``           — DB reachable;
  * ``db_migration`` — the DB's alembic revision equals the repo migration head
                       (catches a stale image that skipped a migration);
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
import tempfile
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Each probe gets its own short timeout so one slow dependency can't make the
# health endpoint itself unresponsive.
_PROBE_TIMEOUT = 2.0

# The repo's alembic head. Readiness asserts the DB's current revision == this.
# Bump this when a new migration is added (the gate will then require the DB to
# have caught up before /ready returns 200).
REPO_MIGRATION_HEAD = "0010_artifacts"

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
        from app.db import AsyncSessionLocal
        from sqlalchemy import text

        async with AsyncSessionLocal() as db:
            await asyncio.wait_for(db.execute(text("SELECT 1")), timeout=_PROBE_TIMEOUT)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("health: DB check failed: %s", exc)
        return False


async def _check_db_migration() -> dict[str, Any]:
    """Assert the DB's alembic revision equals :data:`REPO_MIGRATION_HEAD`.

    Falls back to ok=True with a clear reason when the DB is the test/dev
    in-memory store (no ``alembic_version`` table — built via ``create_all``).
    """
    try:
        from app.db import AsyncSessionLocal
        from sqlalchemy import text

        async with AsyncSessionLocal() as db:
            try:
                row = await asyncio.wait_for(
                    db.execute(text("SELECT version_num FROM alembic_version")),
                    timeout=_PROBE_TIMEOUT,
                )
                current = row.scalar_one_or_none()
            except Exception as inner:  # noqa: BLE001 — table missing etc.
                # Dev/test DBs built via create_all have no alembic_version.
                return _fail(
                    "alembic_version table not present "
                    "(db built via create_all; migration head not tracked)"
                )
            if current is None:
                return _fail("alembic_version row is empty")
            if str(current) != REPO_MIGRATION_HEAD:
                return _fail(
                    f"migration head mismatch: db={current!r} repo={REPO_MIGRATION_HEAD!r}"
                )
            return _ok(f"head={current}")
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
        from app.db import AsyncSessionLocal
        from app.models.model_config import ModelConfig
        from sqlalchemy import select, func

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
