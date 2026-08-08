"""AgentLifecycleEmitter: the single source of truth for multi-agent state.

Holds the :class:`~app.agents.graph.AgentGraph` and translates each lifecycle
transition into:

  * a mutation of the in-memory graph (so :meth:`snapshot` is always current),
  * one or more :class:`~app.agents.schemas.AgentEvent` objects pushed onto the
    run's :class:`~app.agents.stage_context.StageContext` queue (thread-safe),
  * (via the caller) a persisted ``graph_state`` snapshot on the AgentRun row.

State-transition rules enforced here (so the UI can never be lied to):

  * **No two running in a sequential stage.** :meth:`agent_started` requires
    the node's predecessors to be ``completed`` (join semantics). For a pure
    sequential graph this means exactly one running at a time.
  * **Parallel stages may have several running.** :meth:`agent_started` allows
    multiple running when their predecessor edges are all ``completed`` and the
    nodes share a stage — genuine concurrency.
  * **No regression.** A node that is ``completed``/``failed``/``cancelled`` is
    never flipped back to ``running`` by a late event (logged + dropped).
  * **Edges mirror handoffs.** When an agent completes, its outbound edges go
    ``active`` then ``completed``; the downstream node becomes eligible.

The emitter is DB-agnostic — it only mutates the graph + emits events. The
runtime calls :meth:`snapshot` and persists it (cheap; called per transition).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app.agents.graph import (
    AgentGraph,
    AgentGraphNode,
    AgentGraphEdge,
    AgentNodeStatus,
    AgentEdgeStatus,
)
from app.agents.schemas import (
    AgentEvent,
    ev_agent_edge,
    ev_agent_graph,
    ev_agent_status,
    ev_run_status,
)
from app.agents.stage_context import StageContext

logger = logging.getLogger(__name__)

# Terminal statuses — a node in one of these must not move backwards.
_TERMINAL = {AgentNodeStatus.completed, AgentNodeStatus.failed, AgentNodeStatus.cancelled}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentLifecycleEmitter:
    """Owns the graph state for one run and emits lifecycle events."""

    def __init__(self, *, run_id: uuid.UUID, graph: AgentGraph, stage_ctx: StageContext) -> None:
        self.run_id = run_id
        self.graph = graph
        self.graph.run_id = str(run_id)
        self.ctx = stage_ctx
        self._started_at: str | None = None
        self._finished_at: str | None = None
        # Per-node monotonic start timestamps (monotonic clock for duration_ms).
        import time as _time
        self._time = _time
        self._node_starts: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # Topology
    # ------------------------------------------------------------------ #
    def emit_graph_initialized(self) -> None:
        """Send the full topology once. Called at run start, before any agent."""
        self.graph.status = "pending"
        self._emit(ev_agent_graph(run_id=self.run_id, graph=self.graph.to_public_dict()))

    # ------------------------------------------------------------------ #
    # Run status
    # ------------------------------------------------------------------ #
    def emit_run_status(self, status: str) -> None:
        if status == "running" and self._started_at is None:
            self._started_at = _now_iso()
            self.graph.started_at = self._started_at
        if status in ("completed", "failed", "cancelled") and self._finished_at is None:
            self._finished_at = _now_iso()
            self.graph.finished_at = self._finished_at
        self.graph.status = status
        self._emit(ev_run_status(
            run_id=self.run_id, status=status,
            current_agent_ids=self.graph.recompute_active(),
        ))

    # ------------------------------------------------------------------ #
    # Agent transitions
    # ------------------------------------------------------------------ #
    def emit_agent_started(self, agent_id: str, *, task_title: str | None = None) -> bool:
        """Mark a node running. Returns False (no-op) if it can't/shouldn't.

        Guards:
          * terminal nodes never regress to running,
          * join nodes only start when all predecessor edges are completed.
        """
        node = self.graph.node(agent_id)
        if node is None:
            logger.warning("agent_started: unknown node %s", agent_id)
            return False
        if node.status in _TERMINAL:
            logger.info("agent_started: drop (terminal) node=%s status=%s", agent_id, node.status)
            return False
        # Join guard: every inbound edge must be completed.
        # An edge from a not-yet-completed predecessor means we must wait.
        inbound_edges = [e for e in self.graph.edges if e.target == agent_id]
        if inbound_edges and any(e.status != AgentEdgeStatus.completed for e in inbound_edges):
            # If predecessors are themselves still running/pending, this is a
            # legitimate wait — mark waiting instead of running.
            if node.status != AgentNodeStatus.waiting:
                node.status = AgentNodeStatus.waiting
                self._emit(ev_agent_status(
                    run_id=self.run_id, agent_id=agent_id, status=AgentNodeStatus.waiting.value,
                ))
            return False

        node.status = AgentNodeStatus.running
        node.started_at = _now_iso()
        self._node_starts[agent_id] = self._time.monotonic()
        if task_title:
            node.task_title = task_title
        self._emit(ev_agent_status(
            run_id=self.run_id, agent_id=agent_id, status=AgentNodeStatus.running.value,
            task_title=task_title, started_at=node.started_at,
        ))
        self._emit(ev_run_status(
            run_id=self.run_id, status=self.graph.status or "running",
            current_agent_ids=self.graph.recompute_active(),
        ))
        return True

    def emit_agent_waiting(self, agent_id: str, reason: str = "等待其他 Agent") -> None:
        node = self.graph.node(agent_id)
        if node is None or node.status in _TERMINAL:
            return
        node.status = AgentNodeStatus.waiting
        self._emit(ev_agent_status(
            run_id=self.run_id, agent_id=agent_id,
            status=AgentNodeStatus.waiting.value, task_title=reason,
        ))

    def emit_agent_completed(
        self, agent_id: str, *, output_summary: str | None = None
    ) -> None:
        node = self.graph.node(agent_id)
        if node is None:
            return
        if node.status == AgentNodeStatus.completed:
            return  # idempotent
        node.status = AgentNodeStatus.completed
        node.finished_at = _now_iso()
        start = self._node_starts.pop(agent_id, None)
        if start is not None:
            node.duration_ms = int((self._time.monotonic() - start) * 1000)
        if output_summary:
            node.output_summary = output_summary
        self._emit(ev_agent_status(
            run_id=self.run_id, agent_id=agent_id, status=AgentNodeStatus.completed.value,
            finished_at=node.finished_at, duration_ms=node.duration_ms,
            output_summary=output_summary,
        ))
        # Activate outbound handoff edges (evidence/result handed off).
        for e in self.graph.edges:
            if e.source == agent_id and e.status == AgentEdgeStatus.pending:
                self._set_edge(e, AgentEdgeStatus.active)
                # Immediately complete the handoff edge (the data is available
                # now; the downstream node will start when ALL its inbound edges
                # are completed).
                self._set_edge(e, AgentEdgeStatus.completed)
        self._emit(ev_run_status(
            run_id=self.run_id, status=self.graph.status or "running",
            current_agent_ids=self.graph.recompute_active(),
        ))

    def emit_agent_failed(self, agent_id: str, *, error: str) -> None:
        node = self.graph.node(agent_id)
        if node is None:
            return
        node.status = AgentNodeStatus.failed
        node.finished_at = _now_iso()
        node.error = error
        start = self._node_starts.pop(agent_id, None)
        if start is not None:
            node.duration_ms = int((self._time.monotonic() - start) * 1000)
        self._emit(ev_agent_status(
            run_id=self.run_id, agent_id=agent_id, status=AgentNodeStatus.failed.value,
            finished_at=node.finished_at, duration_ms=node.duration_ms, error=error,
        ))
        # Fail-fast: outbound edges go failed; downstream nodes that depended
        # solely on this one are cancelled (see cancel_downstream).
        for e in self.graph.edges:
            if e.source == agent_id and e.status != AgentEdgeStatus.completed:
                self._set_edge(e, AgentEdgeStatus.failed)
        self._emit(ev_run_status(
            run_id=self.run_id, status=self.graph.status or "failed",
            current_agent_ids=self.graph.recompute_active(),
        ))
        # Cascade: cancel every not-yet-started node reachable from here. Already
        # running siblings (parallel) are left alone so their own completion is
        # still reported honestly.
        self.cancel_downstream(agent_id)

    def emit_agent_cancelled(self, agent_id: str) -> None:
        node = self.graph.node(agent_id)
        if node is None or node.status in _TERMINAL:
            return
        node.status = AgentNodeStatus.cancelled
        node.finished_at = _now_iso()
        self._emit(ev_agent_status(
            run_id=self.run_id, agent_id=agent_id, status=AgentNodeStatus.cancelled.value,
            finished_at=node.finished_at,
        ))

    def cancel_downstream(self, failed_agent_id: str) -> None:
        """Fail-fast policy: cancel every node reachable from a failed agent
        that hasn't started yet. Already-running siblings are left alone."""
        reachable: set[str] = set()
        stack = [failed_agent_id]
        while stack:
            cur = stack.pop()
            for e in self.graph.edges:
                if e.source == cur and e.target not in reachable:
                    reachable.add(e.target)
                    stack.append(e.target)
        for nid in reachable:
            node = self.graph.node(nid)
            if node and node.status not in _TERMINAL and node.status != AgentNodeStatus.running:
                self.emit_agent_cancelled(nid)

    # ------------------------------------------------------------------ #
    # Tool attribution (current tool on a node card)
    # ------------------------------------------------------------------ #
    def set_current_tool(self, agent_id: str, *, call_id: str, name: str, status: str) -> None:
        node = self.graph.node(agent_id)
        if node is None:
            return
        node.current_tool = {"call_id": call_id, "name": name, "status": status}

    def clear_current_tool(self, agent_id: str) -> None:
        node = self.graph.node(agent_id)
        if node is None:
            return
        node.current_tool = None

    # ------------------------------------------------------------------ #
    # Snapshot (for persistence)
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        """Return the live graph state as a dict for ``AgentRun.graph_state``."""
        self.graph.recompute_active()
        return self.graph.to_public_dict()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _set_edge(self, edge: AgentGraphEdge, status: AgentEdgeStatus) -> None:
        if edge.status == status:
            return
        edge.status = status
        self._emit(ev_agent_edge(
            run_id=self.run_id, edge_id=edge.id, status=status.value, label=edge.label,
        ))

    def _emit(self, event: AgentEvent) -> None:
        self.ctx.emit(event)
