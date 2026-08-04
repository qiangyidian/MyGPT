"""Agent addressing scheme + inter-agent message protocol (Codex pattern).

Three small, stdlib-only pieces:

* :class:`AgentPath` — a filesystem-like hierarchical agent address
  (``/root``, ``/root/researcher``). The address space is rooted at ``/``;
  every other path is a descendant. Each segment must match ``[A-Za-z0-9_-]+``,
  so an address is always a safe identifier chain (no empty segments, slashes,
  dots, or whitespace).
* :class:`InterAgentMessage` + :data:`SubagentStatus` — the typed envelope a
  subagent uses to talk back to its parent (MESSAGE / NEW_TASK / FINAL_ANSWER),
  plus the lifecycle states a tracked subagent can be in.
* :func:`subagent_notification_fragment` — renders a child's status change as a
  tagged :class:`~app.agents.context_fragments.ContextFragment`
  (``<subagent_notification>{json}</subagent_notification>``) so a parent agent
  *sees* child status changes as injected context — the Codex
  "subagent-status" fragment in miniature.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from app.agents.context_fragments import ContextFragment


# --------------------------------------------------------------------------- #
# Segment validation
# --------------------------------------------------------------------------- #
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_segment(segment: str) -> str:
    """Return ``segment`` if it is one legal address segment, else raise.

    A segment must be a non-empty run of letters, digits, ``_`` or ``-``.
    This rejects ``""`` (empty / double-slash / trailing-slash), whitespace,
    dots, and embedded slashes — i.e. everything that would make an address
    ambiguous or unsafe to embed in a prompt.
    """
    if not isinstance(segment, str) or not _SEGMENT_RE.match(segment):
        raise ValueError(f"invalid agent path segment: {segment!r}")
    return segment


# --------------------------------------------------------------------------- #
# AgentPath — hierarchical agent address
# --------------------------------------------------------------------------- #
class AgentPath:
    """A hierarchical agent address, e.g. ``/root`` or ``/root/researcher``.

    The address space is rooted at ``/`` (the empty path). ``/root`` is a
    top-level agent under the root; ``/root/researcher`` is its child. Every
    segment must match ``[A-Za-z0-9_-]+``.

    Construct from a string — ``AgentPath("/root")`` — or via
    :meth:`parse`. Instances are immutable and hashable, so they work as dict
    keys / set members in an agent registry.
    """

    __slots__ = ("_segments",)

    def __init__(self, path: str = "/") -> None:
        self._segments: tuple[str, ...] = self._parse(path)

    @staticmethod
    def _parse(path: str) -> tuple[str, ...]:
        if not isinstance(path, str):
            raise ValueError(
                f"agent path must be a string, got {type(path).__name__}"
            )
        if not path.startswith("/"):
            raise ValueError(f"agent path must be absolute (start with '/'): {path!r}")
        if path == "/":
            return ()
        # A leading '/' splits to a leading '' — drop it, then validate the rest.
        # This makes '/a//b' (empty middle) and '/root/' (empty trailing) raise.
        segments = path.split("/")[1:]
        for seg in segments:
            _validate_segment(seg)
        return tuple(segments)

    @classmethod
    def parse(cls, path: str) -> "AgentPath":
        """Parse an absolute path string (``"/a/b"``) into an :class:`AgentPath`."""
        return cls(path)

    @classmethod
    def _from_segments(cls, segments: tuple[str, ...]) -> "AgentPath":
        """Build from an already-validated tuple of segments (internal helper)."""
        out = cls.__new__(cls)
        out._segments = tuple(segments)
        for seg in out._segments:
            _validate_segment(seg)
        return out

    # -- accessors ---------------------------------------------------------- #
    @property
    def segments(self) -> tuple[str, ...]:
        """The address segments as a tuple (empty for the root ``/``)."""
        return self._segments

    @property
    def is_root(self) -> bool:
        """True iff this is the global root ``/`` (it has no parent)."""
        return len(self._segments) == 0

    @property
    def parent(self) -> "AgentPath | None":
        """The parent address, or ``None`` for the root ``/``."""
        if self.is_root:
            return None
        return AgentPath._from_segments(self._segments[:-1])

    def join(self, child: str) -> "AgentPath":
        """Return a new path with ``child`` appended as the last segment.

        ``child`` must be a single valid segment (no slashes).
        """
        _validate_segment(child)
        return AgentPath._from_segments(self._segments + (child,))

    def relative_to(self, other: "AgentPath | str") -> str:
        """Return the portion of this path below ``other``.

        The result is a slash-joined string with no leading slash:
        ``AgentPath("/root/researcher").relative_to("/root")`` -> ``"researcher"``;
        ``...relative_to("/")`` -> ``"root/researcher"``.

        Raises :class:`ValueError` if ``other`` is not an ancestor of this path.
        """
        other_path = other if isinstance(other, AgentPath) else AgentPath(other)
        other_segs = other_path._segments
        n = len(other_segs)
        if len(self._segments) < n or self._segments[:n] != other_segs:
            raise ValueError(f"{self!s} is not a descendant of {other_path!s}")
        return "/".join(self._segments[n:])

    # -- dunders ------------------------------------------------------------ #
    def __str__(self) -> str:
        return "/" + "/".join(self._segments)

    def __repr__(self) -> str:
        return f"AgentPath({str(self)!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, AgentPath) and other._segments == self._segments

    def __hash__(self) -> int:
        return hash(self._segments)


# --------------------------------------------------------------------------- #
# Inter-agent message protocol
# --------------------------------------------------------------------------- #
InterAgentMessageType = Literal["MESSAGE", "NEW_TASK", "FINAL_ANSWER"]


@dataclass(frozen=True)
class InterAgentMessage:
    """A typed envelope one agent sends to another (or back to its parent).

    Mirrors Codex's inter-agent message shape: a small fixed set of message
    types, the task/role name this concerns, the sender's address, and a free
    text payload.

    * ``MESSAGE``      — an intermediate note / question to the parent.
    * ``NEW_TASK``     — delegate a fresh sub-task downward.
    * ``FINAL_ANSWER`` — a child's completed result, ready to surface.
    """

    msg_type: InterAgentMessageType
    task_name: str
    sender: AgentPath
    payload: str


# --------------------------------------------------------------------------- #
# Subagent lifecycle status
# --------------------------------------------------------------------------- #
SubagentStatus = Literal[
    "PendingInit", "Running", "Interrupted", "Completed", "Errored", "Shutdown"
]

#: Runtime-checkable set of valid :data:`SubagentStatus` values.
SUBAGENT_STATUSES: frozenset[str] = frozenset(
    {"PendingInit", "Running", "Interrupted", "Completed", "Errored", "Shutdown"}
)


# --------------------------------------------------------------------------- #
# Subagent-status context fragment (the Codex "make child status visible" trick)
# --------------------------------------------------------------------------- #
def subagent_notification_fragment(
    agent_path: AgentPath, status: str, detail: str = ""
) -> ContextFragment:
    """Render a subagent's status change as a tagged context fragment.

    The body is a JSON blob ``{"agent_path": ..., "status": ..., "detail": ...}``
    so a parent agent consumes child status changes as ordinary injected context
    (recognized via the ``<subagent_notification>`` block tag), exactly the way
    Codex surfaces subagent status to the orchestrator.

    Args:
        agent_path: The subagent's address (coerced from str if needed).
        status:     One of :data:`SubagentStatus` — validated here so a typo in
                    the protocol fails loud and early.
        detail:     Optional human-readable detail (may be empty).

    Returns:
        A :class:`ContextFragment` whose ``render()`` produces a
        ``<subagent_notification>{...json...}</subagent_notification>`` block.
    """
    if status not in SUBAGENT_STATUSES:
        raise ValueError(f"unknown subagent status: {status!r}")
    if not isinstance(agent_path, AgentPath):
        agent_path = AgentPath(agent_path)

    body = json.dumps(
        {
            "agent_path": str(agent_path),
            "status": status,
            "detail": detail,
        },
        ensure_ascii=False,
    )
    return ContextFragment(
        name="subagent_notification",
        tag="subagent_notification",
        body=body,
    )
