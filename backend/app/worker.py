"""Worker process entry point: ``python -m app.worker``.

Builds the app context (settings, DB engine), picks the run queue transport
from ``BACKGROUND_WORKER``, and runs the worker loop with graceful shutdown on
SIGTERM/SIGINT. The knowledge-base ingestion queue is drained in the same
process. The worker claims runs from the queue, acquires leases, and
executes each run via :func:`app.agents.workflow.execution.execute_run`.

Two reliability loops live here besides the run loop:

* a **self-check** that proves the event loop is still being scheduled and the
  queue transport is still answering, and parks the result in a heartbeat file
  the container healthcheck reads (``python -m app.healthcheck worker``) —
  a PID-alive check cannot see a loop wedged on a provider timeout, which is the
  failure this worker actually has;
* the **leader-gated retention sweep**, so housekeeping continues even while
  every API replica is being rolled. The advisory lock is shared with the API's
  own sweeper under the same name, so exactly one of the two processes runs it.

Usage::

    python -m app.worker                # durable mode (Redis or in-memory)
    BACKGROUND_WORKER=inprocess python -m app.worker  # single-worker in-memory
"""
from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from app.agents.workflow.execution import execute_run
from app.agents.workflow.queue import get_run_queue
from app.agents.workflow.worker import RunWorker
from app.core.config import get_settings
from app.db import AsyncSessionLocal
from app.healthcheck import beat

logger = logging.getLogger(__name__)

# Self-check cadence. The healthcheck's stale budget is derived from this, so
# the two must stay in sync (see app/healthcheck.py::run).
SELF_CHECK_INTERVAL_SECONDS = 20


async def _self_check(queue: Any, stop_event: asyncio.Event) -> None:
    """Beat while the loop is scheduled AND the queue transport answers.

    Runs as a sibling task on the same event loop as the run loop: if the
    worker wedges inside a blocking call, this task stops being scheduled and
    the heartbeat goes stale — which is precisely the signal a ``kill(1, 0)``
    style probe cannot produce. ``pending_ids`` is used as the probe because it
    exercises the real dependency (Redis Streams consumer group), not just a
    PING to a Redis that may have lost the queue.
    """
    while not stop_event.is_set():
        healthy = True
        detail: dict[str, Any] = {"probe": "ok"}
        try:
            await asyncio.wait_for(queue.pending_ids(), timeout=10)
        except Exception as exc:
            healthy = False
            detail["probe"] = f"{type(exc).__name__}: {exc}"
            logger.warning("worker self-check: queue probe failed: %s", exc)
        if healthy:
            # Deliberately NOT beating on a failed probe: a stale heartbeat is
            # the failure signal, and refreshing it on a dead transport would
            # keep the container "healthy" while it can no longer consume.
            beat("worker", detail=detail, ticks=1)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=SELF_CHECK_INTERVAL_SECONDS)
        except TimeoutError:
            pass


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

    # Knowledge-base ingestion shares this process: it is a DB-leased queue, so
    # it needs no broker, and running it here means a document uploaded right
    # before an API restart still gets indexed while the API is back.
    from app.services import ingestion_queue

    ingestion_worker = await ingestion_queue.build_worker()
    ingestion_task = asyncio.create_task(
        ingestion_worker.run_forever(stop_event=stop_event)
    )

    from app.services.retention import RetentionSweeper

    retention_sweeper = RetentionSweeper(AsyncSessionLocal, leader_name="retention")
    retention_sweeper.start()

    check_task = asyncio.create_task(_self_check(queue, stop_event))
    try:
        await worker.run_forever(stop_event=stop_event)
        await ingestion_task
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        check_task.cancel()
        await asyncio.gather(check_task, return_exceptions=True)
        await retention_sweeper.stop()
    logger.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
