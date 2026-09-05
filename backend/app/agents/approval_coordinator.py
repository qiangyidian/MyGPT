"""In-process coordinator for human-in-the-loop tool approvals.

When a dangerous tool needs approval, the native runtime registers a
:class:`WaitingRun` here and awaits its event — the live SSE stream pauses
(without buffering), the frontend shows an approval card, and the user's
approve/reject/cancel API call signals the event so the run resumes from the
exact step it paused at.

Scope note: this is in-process, so it works under a single uvicorn worker (the
dev/default deployment). For multi-worker prod, route the signal through Redis
pub/sub keyed by ``run_id``. The persisted ``ToolApproval`` / ``AgentRun`` rows
are the durable source of truth either way.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# How long a run waits for a human decision before auto-rejecting (so a paused
# stream can't hold resources forever if the user walks away). Client disconnect
# also cancels the wait via CancelledError.
DEFAULT_WAIT_TIMEOUT = 300.0


@dataclass
class WaitingRun:
    """One paused tool call awaiting a human decision."""

    run_id: uuid.UUID
    approval_id: uuid.UUID
    tool_name: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    decision: str = "pending"  # pending | approved | rejected | cancelled | timed_out
    reason: str = ""


class ApprovalCoordinator:
    """Registry of paused runs, keyed by approval_id."""

    def __init__(self) -> None:
        self._waiting: dict[uuid.UUID, WaitingRun] = {}

    def register(self, *, run_id: uuid.UUID, approval_id: uuid.UUID, tool_name: str) -> WaitingRun:
        wr = WaitingRun(run_id=run_id, approval_id=approval_id, tool_name=tool_name)
        self._waiting[approval_id] = wr
        logger.info("run %s waiting on approval %s for tool %s", run_id, approval_id, tool_name)
        return wr

    def is_waiting(self, approval_id: uuid.UUID) -> bool:
        wr = self._waiting.get(approval_id)
        return wr is not None and wr.decision == "pending"

    def approve(self, approval_id: uuid.UUID) -> bool:
        wr = self._waiting.get(approval_id)
        if wr is None or wr.decision != "pending":
            return False
        wr.decision = "approved"
        wr.event.set()
        return True

    def reject(self, approval_id: uuid.UUID, reason: str = "") -> bool:
        wr = self._waiting.get(approval_id)
        if wr is None or wr.decision != "pending":
            return False
        wr.decision = "rejected"
        wr.reason = reason or "rejected by user"
        wr.event.set()
        return True

    def cancel_run(self, run_id: uuid.UUID) -> int:
        """Cancel every pending wait for a run (e.g. user hit stop). Returns count."""
        n = 0
        for wr in self._waiting.values():
            if wr.run_id == run_id and wr.decision == "pending":
                wr.decision = "cancelled"
                wr.event.set()
                n += 1
        return n

    def release(self, approval_id: uuid.UUID) -> None:
        self._waiting.pop(approval_id, None)

    def waiting_for_run(self, run_id: uuid.UUID) -> WaitingRun | None:
        for wr in self._waiting.values():
            if wr.run_id == run_id and wr.decision == "pending":
                return wr
        return None

    async def wait(self, approval_id: uuid.UUID, timeout: float = DEFAULT_WAIT_TIMEOUT) -> WaitingRun:
        """Block until a decision arrives or the timeout fires.

        Raises ``asyncio.TimeoutError`` on timeout. ``CancelledError`` propagates
        on client disconnect so the stream unwinds cleanly.
        """
        wr = self._waiting[approval_id]
        await asyncio.wait_for(wr.event.wait(), timeout=timeout)
        if wr.decision == "pending":
            wr.decision = "timed_out"
        return wr


# Module-level singleton (single-process).
approval_coordinator = ApprovalCoordinator()
