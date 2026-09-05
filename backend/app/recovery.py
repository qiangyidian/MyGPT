"""Recovery process entry point: ``python -m app.recovery``.

Runs :class:`~app.agents.workflow.recovery.RecoveryScheduler.scan()` on startup
and on a schedule (``RECOVERY_SCAN_INTERVAL_SECONDS``). Each scan finds runs
whose lease has expired (or legacy ``running`` rows with no lease), requeues
retryable runs, and terminally fails exhausted ones.

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
from app.db import AsyncSessionLocal

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
    try:
        while not stop_event.is_set():
            try:
                acted = await scheduler.scan()
                if acted:
                    logger.info("recovery scan: acted on %d run(s)", len(acted))
            except Exception:
                logger.exception("recovery scan failed")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    except KeyboardInterrupt:
        pass
    logger.info("recovery scheduler stopped")


if __name__ == "__main__":
    asyncio.run(main())
