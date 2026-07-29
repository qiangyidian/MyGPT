"""Cross-worker approval signal bus.

The :class:`~app.agents.approval_coordinator.ApprovalCoordinator` is
in-process: a paused SSE stream on worker A can't be woken by an approve
request that lands on worker B. This module adds a Redis pub/sub layer so the
signal crosses workers, with automatic fallback to in-process when Redis is
unavailable (dev / single-worker / tests).

Design:

  * Each worker runs one subscriber task (lazily started) listening on the
    ``approval-signal`` channel for messages ``{approval_id}|{decision}|{reason}``.
  * :meth:`publish` (called by the approve/reject API) writes the decision to
    Redis AND signals the local in-process coordinator. That way the worker
    hosting the paused stream receives it regardless of which worker served the
    API request.
  * If Redis is unreachable, :meth:`publish` degrades to local-only (single
    worker), and :meth:`start_subscriber` is a no-op — the in-process
    coordinator still works for that case.

The bus is a singleton; the subscriber task is started on first use.
"""
from __future__ import annotations

import asyncio
import logging
import json
from typing import Any

from app.agents.approval_coordinator import approval_coordinator
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

_CHANNEL = "approval-signal"


class ApprovalBus:
    """Redis-backed approval signal bus with in-process fallback."""

    def __init__(self) -> None:
        self._sub_task: asyncio.Task | None = None
        self._started = False
        self._redis_ok: bool | None = None  # None = not probed yet

    # ------------------------------------------------------------------ #
    async def publish(
        self, *, approval_id: str, decision: str, reason: str = ""
    ) -> None:
        """Broadcast a decision. Always signals the local coordinator too, so a
        single-worker deployment (or a Redis outage) still works."""
        # Local signal first (fast path; works without Redis).
        self._signal_local(approval_id, decision, reason)
        # Then fan out to other workers via Redis (best-effort).
        if await self._redis_available():
            try:
                client = get_redis()
                await client.publish(
                    _CHANNEL, json.dumps({"approval_id": approval_id, "decision": decision, "reason": reason})
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("approval bus publish failed (local still signaled): %s", exc)

    def _signal_local(self, approval_id: str, decision: str, reason: str) -> None:
        try:
            if decision == "approved":
                approval_coordinator.approve(_to_uuid(approval_id))
            elif decision == "rejected":
                approval_coordinator.reject(_to_uuid(approval_id), reason)
            elif decision == "cancelled":
                approval_coordinator.cancel_run(_to_uuid(approval_id))
        except Exception:  # noqa: BLE001 — local signal is best-effort
            logger.debug("local approval signal no-op for %s", approval_id, exc_info=True)

    # ------------------------------------------------------------------ #
    async def start_subscriber(self) -> None:
        """Start the Redis subscriber task (once per process). No-op if Redis
        is unavailable — the in-process coordinator covers single-worker use."""
        if self._started:
            return
        self._started = True
        if not (await self._redis_available()):
            logger.info("approval bus: Redis unavailable, running in-process only")
            return
        self._sub_task = asyncio.create_task(self._run_subscriber())
        logger.info("approval bus: Redis subscriber started on channel %s", _CHANNEL)

    async def stop(self) -> None:
        if self._sub_task is not None:
            self._sub_task.cancel()
            try:
                await self._sub_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._sub_task = None
        self._started = False

    async def _run_subscriber(self) -> None:
        """Subscribe and forward remote decisions to the local coordinator."""
        from redis.asyncio import PubSub
        client = get_redis()
        pubsub: PubSub = client.pubsub()
        await pubsub.subscribe(_CHANNEL)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    data = json.loads(message.get("data", "{}"))
                    self._signal_local(
                        data.get("approval_id", ""),
                        data.get("decision", ""),
                        data.get("reason", ""),
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("approval bus: bad message %r", message.get("data"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — stay alive across transient errors
            logger.exception("approval bus subscriber crashed: %s", exc)
        finally:
            try:
                await pubsub.unsubscribe(_CHANNEL)
                await pubsub.aclose()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    async def _redis_available(self) -> bool:
        """Probe Redis once (cached). Never raises."""
        if self._redis_ok is not None:
            return self._redis_ok
        try:
            client = get_redis()
            await client.ping()
            self._redis_ok = True
        except Exception:  # noqa: BLE001
            self._redis_ok = False
        return self._redis_ok


def _to_uuid(value: str) -> Any:
    """Best-effort conversion; the coordinator keys by uuid but accepts str-like."""
    try:
        import uuid as _uuid
        return _uuid.UUID(str(value))
    except (ValueError, TypeError):
        return value


# Singleton. Started on app startup (see app/main.py lifespan); the approve/
# reject API calls approval_bus.publish(...).
approval_bus = ApprovalBus()
