"""Cross-thread approval pause/resume for the CrewAI multi-agent path.

Problem: CrewAI calls a tool's ``_run`` from a worker thread (see
:func:`app.agents.adapters.tool_adapter._bridge_async`). When that tool is
dangerous and needs human approval, we must (a) emit ``agent_status: waiting`` +
``run_status: waiting_approval`` on the *main* loop, (b) block the worker thread
until the user decides, (c) resume the agent on approve (re-run via the gateway,
which now finds the approved :class:`~app.models.ToolApproval`).

The :class:`ApprovalCoordinator` is main-loop-bound (``asyncio.Event``), so the
worker thread can't await it directly. :class:`ApprovalBridge` bridges that:

  * Worker thread calls :meth:`request_pause` (sync) → schedules an async
    ``_handle_pause`` on the main loop via ``call_soon_threadsafe`` → blocks on
    a ``threading.Event``.
  * ``_handle_pause`` (main loop) emits the waiting events, registers with the
    :class:`~app.agents.approval_coordinator.ApprovalCoordinator`, awaits the
    decision, then unblocks the worker thread.
  * The approve/reject API (any worker) updates the DB row + signals the
    coordinator → ``_handle_pause`` wakes → worker resumes.

While the worker is blocked, the main loop is free (``aexecute_task`` is
suspended awaiting the worker), so it can serve the approve request. The Redis
multi-worker bus (:mod:`app.agents.approval_bus`) makes the signal work even
when the API lands on a different worker.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agents.lifecycle import AgentLifecycleEmitter
    from app.agents.stage_context import StageContext

logger = logging.getLogger(__name__)

# How long a worker thread waits for a decision before giving up (so a paused
# run can't hold a worker forever if the user walks away). Client disconnect
# also cancels the main-loop wait.
_PAUSE_TIMEOUT = 300.0


@dataclass
class _PauseRequest:
    approval_id: uuid.UUID
    agent_id: str
    tool_name: str
    done: threading.Event = field(default_factory=threading.Event)
    decision: str = "timed_out"  # approved | rejected | cancelled | timed_out
    reason: str = ""


class ApprovalBridge:
    """Bridges worker-thread tool execution to main-loop approval coordination."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        stage_ctx: StageContext,
        emitter: AgentLifecycleEmitter | None = None,
        run_id: uuid.UUID | None = None,
    ) -> None:
        self.loop = loop
        self.stage_ctx = stage_ctx
        self.emitter = emitter
        self.run_id = run_id
        self._active: dict[uuid.UUID, _PauseRequest] = {}

    async def request_pause_async(
        self,
        *,
        approval_id: uuid.UUID,
        agent_id: str,
        tool_name: str,
        timeout: float = _PAUSE_TIMEOUT,
    ) -> tuple[str, str]:
        """Async entry: for callers already on the main loop (e.g. the fake
        executor used in tests / demo mode). Emits waiting, awaits the
        coordinator, returns ``(decision, reason)``."""
        req = _PauseRequest(approval_id=approval_id, agent_id=agent_id, tool_name=tool_name)
        self._active[approval_id] = req
        try:
            await self._resolve_pause(req, timeout)
        finally:
            self._active.pop(approval_id, None)
        return req.decision, req.reason

    def request_pause(
        self,
        *,
        approval_id: uuid.UUID,
        agent_id: str,
        tool_name: str,
        timeout: float = _PAUSE_TIMEOUT,
    ) -> tuple[str, str]:
        """Sync entry: for WORKER-THREAD callers (the real CrewAI adapter's
        ``_run`` runs off the main loop). Schedules ``_resolve_pause`` on the
        main loop via ``call_soon_threadsafe`` and blocks the worker on a
        ``threading.Event`` until it resolves. Must NOT be called from the main
        loop (it would deadlock — use :meth:`request_pause_async` instead).
        """
        req = _PauseRequest(approval_id=approval_id, agent_id=agent_id, tool_name=tool_name)
        self._active[approval_id] = req
        try:
            self.loop.call_soon_threadsafe(
                asyncio.ensure_future,
                self._resolve_pause(req, timeout),
            )
        except RuntimeError:
            req.decision = "cancelled"
            req.reason = "run ended"
            req.done.set()
        req.done.wait(timeout=timeout + 5)
        self._active.pop(approval_id, None)
        if not req.done.is_set():
            req.decision = "timed_out"
            req.reason = "approval timed out"
        return req.decision, req.reason

    async def _resolve_pause(self, req: _PauseRequest, timeout: float) -> None:
        """Core logic shared by the sync + async entry points (runs on the loop).

        Emits ``agent_status: waiting`` + ``run_status: waiting_approval``,
        awaits the :class:`ApprovalCoordinator`, then unblocks the caller.
        """
        from app.agents.approval_coordinator import approval_coordinator

        try:
            if self.emitter is not None:
                self.emitter.emit_agent_waiting(req.agent_id, reason="等待用户确认")
                self.emitter.emit_run_status("waiting_approval")
            wr = approval_coordinator.register(
                run_id=self.run_id or uuid.UUID(int=0),
                approval_id=req.approval_id,
                tool_name=req.tool_name,
            )
            try:
                wr = await approval_coordinator.wait(req.approval_id, timeout=timeout)
            except asyncio.TimeoutError:
                wr.decision = "timed_out"
            finally:
                approval_coordinator.release(req.approval_id)

            req.decision = wr.decision
            req.reason = wr.reason

            # Resume visual state on approve: flip the node back to running.
            if self.emitter is not None and req.decision == "approved":
                from app.agents.graph import AgentNodeStatus
                node = self.emitter.graph.node(req.agent_id)
                if node is not None and node.status == AgentNodeStatus.waiting:
                    node.status = AgentNodeStatus.running
                    self.emitter.emit_run_status("running")
        except Exception:
            logger.exception("approval pause handler failed for %s", req.approval_id)
            req.decision = "cancelled"
            req.reason = "internal error"
        finally:
            req.done.set()

    def cancel_active(self) -> None:
        """Cancel every pending pause (e.g. the run was cancelled)."""
        for req in list(self._active.values()):
            req.decision = "cancelled"
            req.reason = "run cancelled"
            req.done.set()
        self._active.clear()
