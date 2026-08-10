"""High-level persist-first control command writers (Task 4).

The agent-runs API calls these to PERSIST a durable :class:`~app.models.RunCommand`
BEFORE publishing the live wake-up signal (``run_controls`` / ``approval_bus``).
That ordering guarantees the authoritative DB state is correct even when Redis
is unavailable: the live signal only decides latency, not correctness.

Each function is a thin wrapper over :class:`~app.agents.workflow.repository.CommandStore`
so the API endpoints stay readable and the command payloads are shaped in one place.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.workflow.repository import CommandStore


async def record_pause(db: AsyncSession, run_id: uuid.UUID | str) -> None:
    await CommandStore(db).append(run_id, "pause", {})


async def record_resume(db: AsyncSession, run_id: uuid.UUID | str) -> None:
    await CommandStore(db).append(run_id, "resume", {})


async def record_cancel(db: AsyncSession, run_id: uuid.UUID | str) -> None:
    await CommandStore(db).append(run_id, "cancel", {})


async def record_instruction(
    db: AsyncSession, run_id: uuid.UUID | str, text: str
) -> None:
    await CommandStore(db).append(run_id, "instruction", {"text": text})


async def record_approve(
    db: AsyncSession,
    run_id: uuid.UUID | str,
    approval_id: uuid.UUID | str,
    user_id: uuid.UUID | str | None = None,
) -> None:
    payload: dict[str, Any] = {"approval_id": str(approval_id)}
    if user_id is not None:
        payload["user_id"] = str(user_id)
    await CommandStore(db).append(run_id, "approve", payload)


async def record_reject(
    db: AsyncSession,
    run_id: uuid.UUID | str,
    approval_id: uuid.UUID | str,
    reason: str = "",
    user_id: uuid.UUID | str | None = None,
) -> None:
    payload: dict[str, Any] = {"approval_id": str(approval_id), "reason": reason}
    if user_id is not None:
        payload["user_id"] = str(user_id)
    await CommandStore(db).append(run_id, "reject", payload)
