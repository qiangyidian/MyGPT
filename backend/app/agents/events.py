"""Durable, sequenced event store for agent runs (Task 4).

The :class:`EventStore` appends immutable :class:`~app.models.RunEvent` rows
with a per-run monotonic ``sequence`` allocated inside the caller's transaction,
and replays them in order. This is the authoritative history: replaying the
events for a run reconstructs exactly what happened, regardless of process
state.

Sequence allocation is ``max(sequence) + 1`` per run, flushed within the
session's transaction; the ``(run_id, sequence)`` unique constraint is the
last-line guard against any duplicate under concurrency (a racing inserter
violates the constraint and the caller can retry).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.run_event import RunEvent

logger = logging.getLogger(__name__)


class EventStore:
    """Append-only, per-run sequenced event log backed by ``run_events``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        run_id: uuid.UUID | str,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> RunEvent:
        """Allocate the next per-run sequence and append an event.

        Runs ``max(sequence) + 1`` for this run within the current transaction
        and flushes so the row (and its sequence) is visible to later queries
        in the same transaction. The caller commits.
        """
        run_id = _as_uuid(run_id)
        result = await self._session.execute(
            select(func.coalesce(func.max(RunEvent.sequence), 0)).where(
                RunEvent.run_id == run_id
            )
        )
        next_sequence = int(result.scalar_one()) + 1
        event = RunEvent(
            run_id=run_id,
            sequence=next_sequence,
            event_type=event_type,
            data=dict(data) if data else {},
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def replay(
        self,
        run_id: uuid.UUID | str,
        after_sequence: int = 0,
    ) -> list[RunEvent]:
        """Return this run's events ordered by sequence (optionally after a cursor)."""
        run_id = _as_uuid(run_id)
        result = await self._session.execute(
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.sequence > after_sequence)
            .order_by(RunEvent.sequence)
        )
        return list(result.scalars().all())


async def append_event_safe(
    session: AsyncSession,
    run_id: uuid.UUID | str,
    event_type: str,
    data: dict[str, Any] | None = None,
) -> RunEvent | None:
    """Best-effort :meth:`EventStore.append` that never raises.

    Used at orchestrator boundaries where a failed event append must NEVER break
    the run lifecycle. Returns the persisted row on success, ``None`` otherwise.
    """
    try:
        return await EventStore(session).append(run_id, event_type, data)
    except Exception:  # noqa: BLE001 -- best-effort durability
        logger.debug(
            "durable event append failed (%s for run %s)", event_type, run_id,
            exc_info=True,
        )
        return None


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
