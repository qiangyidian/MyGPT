"""Readiness/liveness health checks for the real dependencies.

The old ``GET /health`` returned ``{"status": "ok"}`` unconditionally — a fake
probe that reported "ready" even when the DB / Redis / Qdrant were down. This
module pings each real dependency (short timeouts, never blocks) so the probe is
meaningful for load-balancer / k8s readiness and for ops dashboards.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Each probe gets its own short timeout so one slow dependency can't make the
# health endpoint itself unresponsive.
_PROBE_TIMEOUT = 2.0


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


async def _check_redis() -> bool:
    try:
        from app.core.redis import get_redis

        client = get_redis()
        await asyncio.wait_for(client.ping(), timeout=_PROBE_TIMEOUT)
        return True
    except Exception as exc:  # noqa: BLE001
        # Redis is optional (refresh revocation / rate limiting degrade gracefully),
        # so report its state but do not fail the whole probe on it.
        logger.debug("health: redis check failed: %s", exc)
        return False


async def _check_qdrant() -> bool:
    try:
        from app.rag.qdrant_store import get_vector_store

        store = get_vector_store()
        collections = await asyncio.wait_for(
            store._client.get_collections(), timeout=_PROBE_TIMEOUT
        )
        return collections is not None
    except Exception as exc:  # noqa: BLE001
        logger.debug("health: qdrant check failed: %s", exc)
        return False


async def check_health() -> dict[str, Any]:
    """Probe DB / Redis / Qdrant concurrently. Returns per-component status.

    The overall ``status`` is ``"ok"`` only when the DB (hard dependency) is up;
    Redis/Qdrant are reported but optional (the app degrades without them).
    """
    db_ok, redis_ok, qdrant_ok = await asyncio.gather(
        _check_db(), _check_redis(), _check_qdrant()
    )
    components = {"db": db_ok, "redis": redis_ok, "qdrant": qdrant_ok}
    return {
        "status": "ok" if db_ok else "degraded",
        "components": components,
    }
