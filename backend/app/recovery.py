"""Recovery process entry point: ``python -m app.recovery``.

Runs :class:`~app.agents.workflow.recovery.RecoveryScheduler.scan()` on startup
and on a schedule (``RECOVERY_SCAN_INTERVAL_SECONDS``). Each scan finds runs
whose lease has expired (or legacy ``running`` rows with no lease), requeues
retryable runs, and terminally fails exhausted ones.

Singleton under leader election: the scan requeues work, so two schedulers
running it concurrently would double-requeue and double-fail runs (each keeps
its own view of "expired"). The loop therefore only ticks while it holds the
``recovery`` Postgres advisory lock (see :mod:`app.core.leader`), which means
this Deployment can be scaled to >1 replica for failover without duplicating the
sweep. The standby promotes within one heartbeat of the leader dying; failover
semantics, including the split-brain argument, are documented there.

Each completed tick also writes a heartbeat file consumed by
``python -m app.healthcheck recovery`` — the container healthcheck needs
evidence of *progress*, not of a live PID.

Usage::

    python -m app.recovery                # durable mode (Redis or in-memory)
"""
from __future__ import annotations

import asyncio
import logging
import signal

from app.agents.workflow.queue import get_run_queue
from app.agents.workflow.recovery import RecoveryScheduler
from app.core.config import get_settings
from app.core.leader import LeaderGate
from app.db import AsyncSessionLocal
from app.healthcheck import beat

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    # Structured logging shared with the API process (JSON in prod) so all
    # three processes emit one parseable, correlation-id-capable format.
    from app.core.logging import configure_logging

    configure_logging("DEBUG" if settings.is_dev else "INFO")
    logger.info(
        "recovery scheduler starting (interval=%ds, max_retries=%d)",
        settings.RECOVERY_SCAN_INTERVAL_SECONDS,
        settings.RUN_MAX_RETRIES,
    )

    queue = await get_run_queue()
    scheduler = RecoveryScheduler(
        session_factory=AsyncSessionLocal,
        queue=queue,
    )
    # One gate per singleton loop; the name is the lock identity, so a second
    # Deployment scaled to 5 replicas contributes 4 standbys and 1 sweeper.
    gate = LeaderGate("recovery", session_factory=AsyncSessionLocal)

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("recovery scheduler received shutdown signal...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, RuntimeError):
            pass

    interval = settings.RECOVERY_SCAN_INTERVAL_SECONDS
    promoted = False
    logged_standby = False
    try:
        while not stop_event.is_set():
            leading = False
            try:
                leading = await gate.acquire()
                if leading:
                    if not promoted:
                        promoted = True
                        logged_standby = False
                        # Records "this container has led at least once", which
                        # is what the healthprobe uses to tell a hung leader from
                        # a standby that was never promoted.
                        beat("recovery-promoted")
                    acted = await scheduler.scan()
                    if acted:
                        logger.info("recovery scan: acted on %d run(s)", len(acted))
                    # Stalled-pending reclaim (finding 41): the stream PEL holds
                    # entries a worker claimed and never finished with, which the
                    # DB-driven scan above structurally cannot see. Leader-gated
                    # here so exactly one process claims per tick.
                    reclaimed = await queue.reclaim_stale("recovery-reclaimer")
                    if reclaimed:
                        logger.info(
                            "stream reclaim: requeued %d stalled entry(ies)", reclaimed
                        )
            except Exception:
                logger.exception("recovery scan failed")
            finally:
                if leading:
                    # A tick that raised still counts as progress: the loop is
                    # alive and sweeping, which is the property the probe checks.
                    beat("recovery", detail={"promoted": promoted})
            if not leading and not logged_standby:
                # Log once per standby period so an operator can tell "waiting
                # for failover" from "misconfigured and permanently locked out".
                logger.info("recovery: standby, waiting for leadership")
                logged_standby = True
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                pass
    except KeyboardInterrupt:
        pass
    finally:
        await gate.release()
    logger.info("recovery scheduler stopped")


if __name__ == "__main__":
    asyncio.run(main())
