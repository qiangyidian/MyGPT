"""Repository over the ``agent_attempts`` table (Task 6).

Task 4 created the :class:`~app.models.AgentAttempt` model + table and reserved
it for per-attempt usage / retry accounting. This module adds the repository
the workflow engine uses to persist each step attempt's lifecycle:
``pending`` -> ``running`` -> ``done`` | ``error``.

Following the Task-4 / Task-5 discipline, the repository operates on the
caller's :class:`~sqlalchemy.ext.asyncio.AsyncSession` and keeps transactions
tight: it only ``flush``es; the caller commits. The engine opens a short-lived
session per attempt (via a session factory) so a run's main flow is never
poisoned by an attempt-persistence failure.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentAttempt


class AttemptRepository:
    """CRUD + lifecycle transitions for :class:`AgentAttempt` rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_pending(
        self,
        run_id: uuid.UUID | str,
        step_key: str,
        attempt_number: int = 1,
    ) -> AgentAttempt:
        """Insert a ``pending`` attempt row and flush (caller commits)."""
        attempt = AgentAttempt(
            run_id=_as_uuid(run_id),
            step_key=step_key,
            attempt_number=max(1, int(attempt_number)),
            status="pending",
        )
        self._session.add(attempt)
        await self._session.flush()
        return attempt

    async def mark_running(self, attempt: AgentAttempt) -> None:
        """Flip a ``pending`` (or ``error``) attempt to ``running``."""
        attempt.status = "running"
        if attempt.started_at is None:
            attempt.started_at = datetime.now(timezone.utc)
        await self._session.flush()

    async def mark_done(
        self,
        attempt: AgentAttempt,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """Flip a ``running`` attempt to ``done`` and record its usage."""
        attempt.status = "done"
        if usage is not None:
            attempt.usage = dict(usage)
        attempt.finished_at = datetime.now(timezone.utc)
        await self._session.flush()

    async def mark_error(
        self,
        attempt: AgentAttempt,
        error: str,
    ) -> None:
        """Flip a ``running`` attempt to ``error`` with a message."""
        attempt.status = "error"
        attempt.error = str(error)
        attempt.finished_at = datetime.now(timezone.utc)
        await self._session.flush()

    async def next_attempt_number(
        self,
        run_id: uuid.UUID | str,
        step_key: str,
    ) -> int:
        """One plus the highest existing attempt_number for this run+step.

        Returns 1 when no prior attempt exists. Used by the engine to number
        retries monotonically per step.
        """
        result = await self._session.execute(
            select(func.coalesce(func.max(AgentAttempt.attempt_number), 0))
            .where(
                AgentAttempt.run_id == _as_uuid(run_id),
                AgentAttempt.step_key == step_key,
            )
        )
        return int(result.scalar_one()) + 1


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
