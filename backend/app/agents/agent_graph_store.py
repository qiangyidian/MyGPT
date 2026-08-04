"""Persisted multi-agent spawn graph (Codex ``agent-graph-store`` pattern).

Codex makes multi-agent topology **durable and queryable** independent of whether
agents are currently live: parent/child spawn edges with ``Open``/``Closed``
lifecycle, ``list_children`` and breadth-first ``list_descendants``. This lets a
UI render/resume the graph and detect orphans.

Storage-neutral: a :class:`AgentGraphStore` protocol + a JSON-file default
(:class:`JsonAgentGraphStore`). Easy to re-back with SQLite/Postgres later by
implementing the protocol.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SpawnEdge:
    parent_id: str
    child_id: str
    status: str = "open"  # "open" | "closed"
    created_at: str = field(default_factory=_now_iso)
    meta: dict = field(default_factory=dict)


@runtime_checkable
class AgentGraphStore(Protocol):
    """Storage-neutral spawn-edge store."""

    def add_edge(self, *, parent_id: str, child_id: str, meta: dict | None = None) -> SpawnEdge: ...

    def children(self, parent_id: str) -> list[SpawnEdge]: ...

    def descendants(self, root_id: str, *, include_closed: bool = True) -> list[SpawnEdge]: ...

    def set_status(self, child_id: str, status: str) -> None: ...


class JsonAgentGraphStore:
    """JSON-file backed :class:`AgentGraphStore`. Atomic writes; BFS descendants."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # -- internals --
    def _read(self) -> list[SpawnEdge]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [SpawnEdge(**e) for e in data.get("edges", [])]

    def _write(self, edges: list[SpawnEdge]) -> None:
        payload = {"edges": [asdict(e) for e in edges]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- API --
    def add_edge(self, *, parent_id: str, child_id: str, meta: dict | None = None) -> SpawnEdge:
        edges = self._read()
        edge = SpawnEdge(parent_id=parent_id, child_id=child_id, meta=meta or {})
        edges.append(edge)
        self._write(edges)
        return edge

    def children(self, parent_id: str) -> list[SpawnEdge]:
        return [e for e in self._read() if e.parent_id == parent_id]

    def descendants(self, root_id: str, *, include_closed: bool = True) -> list[SpawnEdge]:
        """Breadth-first traversal of the subtree under ``root_id`` (exclusive of root)."""
        all_edges = self._read()
        by_parent: dict[str, list[SpawnEdge]] = {}
        for e in all_edges:
            by_parent.setdefault(e.parent_id, []).append(e)
        out: list[SpawnEdge] = []
        seen: set[str] = set()
        queue = [root_id]
        while queue:
            current = queue.pop(0)
            for e in by_parent.get(current, []):
                if not include_closed and e.status != "open":
                    continue
                out.append(e)
                if e.child_id not in seen:
                    seen.add(e.child_id)
                    queue.append(e.child_id)
        return out

    def set_status(self, child_id: str, status: str) -> None:
        if status not in ("open", "closed"):
            raise ValueError(f"status must be open|closed, got {status!r}")
        edges = self._read()
        changed = False
        for e in edges:
            if e.child_id == child_id and e.status != status:
                e.status = status
                changed = True
        if changed:
            self._write(edges)


def new_node_id() -> str:
    """A stable id for an agent node (used as parent_id/child_id in the store)."""
    return uuid.uuid4().hex
