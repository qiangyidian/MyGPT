"""Transactional repositories for durable run commands and leases (Task 4).

Both stores operate on the caller's :class:`~sqlalchemy.ext.asyncio.AsyncSession`
and keep transactions tight: allocations and status transitions flush within
the session's transaction; the caller commits.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.run_command import RunCommand
from app.models.run_lease import RunLease

_DEFAULT_CLAIM_OWNER = "system"


class CommandStore:
    """Exactly-once control-command queue backed by ``run_commands``.

    Lifecycle: ``pending`` -> ``claimed`` -> ``applied`` (or ``failed``).
    ``claim_pending`` atomically selects a run's pending rows and flips them to
    claimed within one transaction, so each command is applied exactly once.
    Rows are retained for auditability.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        run_id: uuid.UUID | str,
        command_type: str,
        payload: dict[str, Any] | None = None,
    ) -> RunCommand:
        command = RunCommand(
            run_id=_as_uuid(run_id),
            command_type=command_type,
            payload=dict(payload) if payload else {},
            status="pending",
        )
        self._session.add(command)
        await self._session.flush()
        return command

    async def claim_pending(
        self,
        run_id: uuid.UUID | str,
        owner: str = _DEFAULT_CLAIM_OWNER,
    ) -> list[RunCommand]:
        """Atomically claim all pending commands for a run (FIFO by created_at).

        Returns the claimed rows (now status=claimed) so the caller can apply
        them. A subsequent call returns ``[]`` — each command is claimed once.
        """
        run_id = _as_uuid(run_id)
        result = await self._session.execute(
            select(RunCommand)
            .where(RunCommand.run_id == run_id, RunCommand.status == "pending")
            .order_by(RunCommand.created_at, RunCommand.id)
        )
        pending = list(result.scalars().all())
        if not pending:
            return []
        now = datetime.now(timezone.utc)
        for command in pending:
            command.status = "claimed"
            command.claimed_at = now
            command.claimed_by = owner
        await self._session.flush()
        return pending

    async def mark_applied(self, command_id: uuid.UUID | str) -> None:
        command = await self._session.get(RunCommand, _as_uuid(command_id))
        if command is None:
            return
        command.status = "applied"
        command.applied_at = datetime.now(timezone.utc)
        await self._session.flush()

    async def mark_failed(
        self, command_id: uuid.UUID | str, error: str
    ) -> None:
        command = await self._session.get(RunCommand, _as_uuid(command_id))
        if command is None:
            return
        command.status = "failed"
        command.error = error
        await self._session.flush()


class LeaseStore:
    """Run execution leases with optimistic fencing (Task 4 store; Task 5 consumes).

    One live lease per run (``run_id`` unique). ``acquire`` inserts-or-overwrites
    with a new monotonic ``version``; ``renew`` extends the expiry and bumps the
    version only if the caller owns the lease; ``release`` deletes it under the
    same owner check. ``is_expired`` is pure logic (no DB).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _get(self, run_id: uuid.UUID) -> RunLease | None:
        result = await self._session.execute(
            select(RunLease).where(RunLease.run_id == run_id)
        )
        return result.scalar_one_or_none()

    async def acquire(
        self,
        run_id: uuid.UUID | str,
        owner: str,
        ttl_seconds: int,
    ) -> RunLease:
        """Insert a new lease, or take over an existing one with a bumped version.

        Acquisition is unconditional: a worker may take over after the previous
        lease expired (ownership/renewal is enforced by :meth:`renew`/:meth:`release`).
        """
        run_id = _as_uuid(run_id)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=max(0, int(ttl_seconds)))
        lease = await self._get(run_id)
        if lease is None:
            lease = RunLease(
                run_id=run_id,
                owner=owner,
                version=1,
                acquired_at=now,
                expires_at=expires_at,
            )
            self._session.add(lease)
        else:
            lease.owner = owner
            lease.version = (lease.version or 0) + 1
            lease.acquired_at = now
            lease.expires_at = expires_at
        await self._session.flush()
        return lease

    async def renew(
        self,
        run_id: uuid.UUID | str,
        owner: str,
        ttl_seconds: int,
    ) -> RunLease | None:
        """Extend the lease expiry + bump version only if the caller owns it."""
        lease = await self._get(_as_uuid(run_id))
        if lease is None or lease.owner != owner:
            return None
        lease.expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=max(0, int(ttl_seconds))
        )
        lease.version = (lease.version or 0) + 1
        await self._session.flush()
        return lease

    async def release(
        self, run_id: uuid.UUID | str, owner: str
    ) -> bool:
        """Delete the lease if the caller owns it. Returns whether a lease was released."""
        lease = await self._get(_as_uuid(run_id))
        if lease is None or lease.owner != owner:
            return False
        await self._session.delete(lease)
        await self._session.flush()
        return True

    def is_expired(
        self, lease: RunLease, now: datetime | None = None
    ) -> bool:
        """Pure logic: True when ``now`` is at/after the lease's expiry."""
        if lease.expires_at is None:
            return False
        if now is None:
            now = datetime.now(timezone.utc)
        return now >= lease.expires_at


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
