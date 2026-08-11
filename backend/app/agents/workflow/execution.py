"""Run execution seam for the durable worker (Task 5).

:func:`execute_run` is the production executor the worker calls after claiming
a run. It delegates to :func:`app.services.chat_service.run_durable_turn`,
which reconstructs the turn context from the persisted run + conversation +
messages, runs it through the existing orchestrator + native runtime, and
yields each :class:`~app.agents.schemas.AgentEvent`. The worker
(:class:`~app.agents.workflow.worker.RunWorker`) persists each event as a
durable :class:`~app.models.RunEvent` and manages the lease lifecycle.

The orchestrator reuses the EXISTING AgentRun (the worker created + leased it
before calling us) instead of creating a duplicate — see
``ctx.extra["durable_run_id"]`` in
:meth:`~app.agents.orchestrator.ChatOrchestrator.stream`.
"""
from __future__ import annotations

import logging
import uuid
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.schemas import AgentEvent

logger = logging.getLogger(__name__)


async def execute_run(
    run_id: uuid.UUID, session: AsyncSession
) -> AsyncIterator[AgentEvent]:
    """Execute a persisted run and yield its events.

    This is the executor the durable worker calls. It loads the run's turn
    context from persisted data, runs it through the orchestrator + runtime,
    and yields each :class:`AgentEvent`. The worker persists each as a durable
    :class:`RunEvent` and manages the lease lifecycle.

    A missing run yields a single terminal ``error`` event so the worker acks
    cleanly instead of spinning.
    """
    # Late import avoids a circular dependency at module load time
    # (chat_service imports from app.agents.* which imports this module's
    # siblings).
    from app.services.chat_service import run_durable_turn

    async for evt in run_durable_turn(run_id, session):
        yield evt
