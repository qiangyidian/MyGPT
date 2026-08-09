"""Short-lived, ID-based persistence for background agent activity."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select, update

from app.agents.db_mutation import rollback_safely
from app.models import AgentRun, Message

SessionFactory = Callable[[], Any]


async def _in_transaction(
    session_factory: SessionFactory,
    operation: Callable[[Any], Awaitable[None]],
) -> None:
    async with session_factory() as session:
        try:
            await operation(session)
            await session.commit()
        except BaseException:
            await rollback_safely(session)
            raise


async def persist_continuation_checkpoint(
    session_factory: SessionFactory,
    *,
    message_id: uuid.UUID,
    run_id: uuid.UUID | None,
    content: str,
    metadata: dict[str, Any],
    checkpoint: dict[str, Any],
) -> None:
    """Persist a full message snapshot and continuation using no ORM instances."""
    checkpoint_snapshot = dict(checkpoint)
    message_metadata = {**metadata, "continuation": checkpoint_snapshot}

    async def operation(session: Any) -> None:
        await session.execute(
            update(Message)
            .where(Message.id == message_id)
            .values(content=content, metadata_=message_metadata)
        )
        if run_id is None:
            return
        result = await session.execute(
            select(AgentRun.output).where(AgentRun.id == run_id)
        )
        current_output = result.scalar_one_or_none()
        round_number = int(checkpoint_snapshot.get("round") or 0)
        resume_marker = f"continuation:{round_number}"
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(
                output={
                    **(current_output or {}),
                    "continuation": checkpoint_snapshot,
                },
                current_step=resume_marker,
                resume_token=resume_marker,
            )
        )

    await _in_transaction(session_factory, operation)


async def persist_graph_snapshot(
    session_factory: SessionFactory,
    *,
    run_id: uuid.UUID,
    snapshot: dict[str, Any],
    definition: bool,
) -> None:
    """Persist a graph snapshot without loading the request-session AgentRun."""

    async def operation(session: Any) -> None:
        values: dict[str, Any] = {"graph_state": snapshot}
        if definition:
            values["graph_definition"] = snapshot
        await session.execute(
            update(AgentRun).where(AgentRun.id == run_id).values(**values)
        )

    await _in_transaction(session_factory, operation)


async def persist_research_plan(
    session_factory: SessionFactory,
    *,
    run_id: uuid.UUID,
    plan: dict[str, Any],
) -> None:
    """Persist a best-effort research plan in an isolated transaction."""

    async def operation(session: Any) -> None:
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(plan=plan, plan_status="draft")
        )

    await _in_transaction(session_factory, operation)


async def persist_terminal_run(
    session_factory: SessionFactory,
    *,
    run_id: uuid.UUID,
    event_kind: str,
    event_data: dict[str, Any],
) -> None:
    """Merge terminal output against the durable row, never a stale ORM object."""

    async def operation(session: Any) -> None:
        result = await session.execute(
            select(AgentRun.output, AgentRun.status).where(AgentRun.id == run_id)
        )
        row = result.one_or_none()
        if row is None:
            return
        current_output, current_status = row
        values: dict[str, Any] = {
            "finished_at": datetime.now(timezone.utc),
            "output": {**(current_output or {}), **event_data},
        }
        if event_kind == "done":
            values["status"] = (
                "cancelled"
                if event_data.get("finish_reason") == "cancelled"
                or current_status == "cancelled"
                else "completed"
            )
        else:
            values["status"] = "failed"
            values["error_message"] = str(event_data.get("message", ""))
        await session.execute(
            update(AgentRun).where(AgentRun.id == run_id).values(**values)
        )

    await _in_transaction(session_factory, operation)
