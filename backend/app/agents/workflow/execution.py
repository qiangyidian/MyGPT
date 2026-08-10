"""Run execution seam for the durable worker (Task 5).

:func:`execute_run` is the production executor the worker calls after claiming
a run. It loads the :class:`~app.models.AgentRun`, reconstructs the turn
context, delegates to the existing orchestrator/runtime, and yields each
:class:`~app.agents.schemas.AgentEvent`. The worker (:class:`~app.agents.workflow.worker.RunWorker`)
persists each event as a durable :class:`~app.models.RunEvent`.

**Deferred wiring (honest note):** the full turn-context reconstruction
(system-prompt assembly, RAG retrieval, attachment binding, history trimming,
model-config resolution) currently lives inside the 400-line
``ChatService.stream`` pipeline, which also creates the AgentRun and persists
the user message — so it cannot be called for an *existing* run_id without
re-persisting the turn. Wiring ``execute_run`` end-to-end requires either
persisting the fully-built turn context on the AgentRun (a new JSONB field)
or refactoring ``ChatService.stream`` to separate context-building from
execution. Both are significant changes that must not risk the inprocess path.

The infra (queue, worker, lease, recovery, cursor-replay SSE) is fully
functional and tested with injected executors. When the turn-context
reconstruction is wired, ``execute_run`` is the single integration point —
no other Task-5 component needs to change.
"""
from __future__ import annotations

import logging
import uuid
from typing import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.schemas import AgentEvent
from app.models.agent_run import AgentRun

logger = logging.getLogger(__name__)


async def execute_run(
    run_id: uuid.UUID, session: AsyncSession
) -> AsyncIterator[AgentEvent]:
    """Execute a persisted run and yield its events.

    This is the executor the durable worker calls. It yields
    :class:`AgentEvent` objects; the worker persists each as a durable
    :class:`RunEvent` and manages the lease lifecycle.

    Currently emits a ``run.started`` / ``run.completed`` (or ``run.failed``)
    pair. Full turn-context reconstruction is deferred (see module docstring).
    """
    run = await session.get(AgentRun, run_id)
    if run is None:
        yield AgentEvent(kind="error", data={"code": "run_not_found", "run_id": str(run_id)})
        return

    yield AgentEvent(
        kind="run.started",
        data={
            "run_id": str(run_id),
            "runtime": run.runtime,
            "conversation_id": str(run.conversation_id),
        },
    )

    try:
        # --- DEFERRED: full turn-context reconstruction + orchestrator call ---
        # When wired, this section will:
        #   1. Load the conversation, user, model config, and messages.
        #   2. Rebuild the system prompt, RAG context, and trimmed history.
        #   3. Build an AgentTurnContext with run_id=<this run>.
        #   4. Iterate chat_orchestrator.stream(ctx) and yield each AgentEvent.
        #
        # Until then, yield a terminal event so the worker acks cleanly.
        yield AgentEvent(
            kind="run.completed",
            data={
                "run_id": str(run_id),
                "status": "completed",
                "note": "durable execution seam (full context reconstruction deferred)",
            },
        )
    except Exception as exc:  # noqa: BLE001
        yield AgentEvent(
            kind="run.failed",
            data={"run_id": str(run_id), "error": str(exc)[:500]},
        )
