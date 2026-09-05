"""Worker process entry point: ``python -m app.worker``.

Builds the app context (settings, DB engine), picks the run queue transport
from ``BACKGROUND_WORKER``, and runs the worker loop with graceful shutdown on
SIGTERM/SIGINT. The worker claims runs from the queue, acquires leases, and
executes each run via :func:`app.agents.workflow.execution.execute_run`.

Usage::

    python -m app.worker                # durable mode (Redis or in-memory)
    BACKGROUND_WORKER=inprocess python -m app.worker  # single-worker in-memory
"""
from __future__ import annotations

import asyncio
import logging
import signal

from app.agents.workflow.execution import execute_run
from app.agents.workflow.queue import get_run_queue
from app.agents.workflow.worker import RunWorker
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
        "worker starting (BACKGROUND_WORKER=%s, stream=%s)",
        settings.BACKGROUND_WORKER,
        settings.RUN_QUEUE_STREAM,
    )

    queue = await get_run_queue()
    worker = RunWorker(
        queue=queue,
        execute_fn=execute_run,
        session_factory=AsyncSessionLocal,
    )

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("worker received shutdown signal, draining...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, RuntimeError):
            # Windows doesn't support add_signal_handler; fall back to KeyboardInterrupt.
            pass

    try:
        await worker.run_forever(stop_event=stop_event)
    except KeyboardInterrupt:
        pass
    logger.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
