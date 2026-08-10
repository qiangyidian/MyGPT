"""Durable, sequenced event store for agent runs (Task 4).

The :class:`EventStore` appends immutable :class:`~app.models.RunEvent` rows
with a per-run monotonic ``sequence`` allocated inside the caller's transaction,
and replays them in order. This is the authoritative history: replaying the
events for a run reconstructs exactly what happened, regardless of process
state.

Sequence allocation is ``max(sequence) + 1`` per run. Under the intended
concurrency model (Task 5), event appending for a run is owned by that run's
LEASE-HOLDER — there is a single effective writer per run, so the ``max+1``
allocation cannot race in normal operation. The ``(run_id, sequence)`` unique
constraint plus a bounded retry inside :meth:`EventStore.append` are
defense-in-depth for the rare concurrent case (e.g. a lease handoff race): if
two appenders both compute the same sequence, one violates the constraint and
:meth:`EventStore.append` re-reads ``max(sequence)`` and retries.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.run_event import RunEvent

logger = logging.getLogger(__name__)

# Bounded retry count for the max(sequence)+1 allocation under the rare
# concurrent-append race. Three attempts is far more than the worst-case
# interleaving needs when there is at most one racing appender.
_MAX_SEQUENCE_ATTEMPTS = 3


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

        On a unique ``(run_id, sequence)`` violation (a concurrent appender won
        the sequence), the failed insert is rolled back to its SAVEPOINT and the
        allocation re-reads ``max(sequence)`` and retries — bounded by
        ``_MAX_SEQUENCE_ATTEMPTS``. This is the defense-in-depth for the
        single-writer-per-run invariant (enforced by Task 5 leases).
        """
        run_id = _as_uuid(run_id)
        payload = dict(data) if data else {}
        last_exc: BaseException | None = None
        for _ in range(_MAX_SEQUENCE_ATTEMPTS):
            try:
                # A SAVEPOINT per attempt isolates a failed insert so the outer
                # transaction stays usable for the retry.
                async with self._session.begin_nested():
                    result = await self._session.execute(
                        select(func.coalesce(func.max(RunEvent.sequence), 0))
                        .where(RunEvent.run_id == run_id)
                    )
                    next_sequence = int(result.scalar_one()) + 1
                    event = RunEvent(
                        run_id=run_id,
                        sequence=next_sequence,
                        event_type=event_type,
                        data=payload,
                    )
                    self._session.add(event)
                    await self._session.flush()
                    return event
            except IntegrityError as exc:
                # The (run_id, sequence) collision means another appender
                # grabbed this sequence; re-read max(sequence) and retry.
                last_exc = exc
                continue
        # Exhausted retries: re-raise so the caller sees the conflict rather
        # than silently dropping the event.
        assert last_exc is not None
        raise last_exc

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
    """Best-effort :meth:`EventStore.append` that never raises and never poisons the session.

    Used at orchestrator boundaries where a failed event append must NEVER break
    the run lifecycle. The append runs inside a SAVEPOINT, so if it fails the
    savepoint is rolled back and the outer transaction stays clean — the run's
    own status commit (which follows) is unaffected. Returns the persisted row
    on success, ``None`` otherwise.
    """
    try:
        async with session.begin_nested():
            return await EventStore(session).append(run_id, event_type, data)
    except Exception:  # noqa: BLE001 -- best-effort durability
        logger.debug(
            "durable event append failed (%s for run %s)", event_type, run_id,
            exc_info=True,
        )
        return None


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
