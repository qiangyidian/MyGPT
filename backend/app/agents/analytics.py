"""Structured analytics fact taxonomy + recorder (Codex pattern).

Codex emits one *typed* fact for every meaningful agent event rather than
free-form log lines, so downstream consumers (telemetry, dashboards,
session-replay, eval harnesses) can rely on a stable discriminator
(``kind``) and a known payload shape per event. This module ports that idea:

* :class:`Fact` — base dataclass: a ``kind`` discriminator, a ``ts_unix``
  capture time (populated at construction via :func:`time.time`), and a
  ``data`` payload dict. :meth:`Fact.to_dict` is the serializable form.
* One concrete dataclass per event family (turn profile, token usage,
  compaction, guardian review, skill invocation, hook run, goal lifecycle,
  mid-turn steer, subagent spawn) — each fixes its ``kind`` and declares a
  few typed fields. The base :meth:`__post_init__` reflects those typed
  fields into ``self.data`` so the instance attribute and ``to_dict()``
  always agree.
* :class:`AnalyticsRecorder` — collects facts, exposes :meth:`drain`, and
  fans each fact out to a pluggable ``sink`` callable (default = log via
  :mod:`logging`; the recorder always keeps its own in-memory buffer too).
  All buffer access is guarded by a lock so concurrent producers are safe.

Stdlib only.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, fields
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Stable kind discriminators (dotted lowercase; never reuse a value).
KIND_TURN_PROFILE = "turn.profile"
KIND_TURN_TOKENS = "turn.tokens"
KIND_COMPACTION = "context.compaction"
KIND_GUARDIAN_REVIEW = "guardian.review"
KIND_SKILL_INVOKE = "skill.invoke"
KIND_HOOK_RUN = "hook.run"
KIND_GOAL_EVENT = "goal.event"
KIND_TURN_STEER = "turn.steer"
KIND_SUBAGENT_STARTED = "subagent.started"

# Base Fact fields that are NOT part of the per-event payload.
_BASE_FIELD_NAMES = frozenset({"kind", "ts_unix", "data"})


@dataclass
class Fact:
    """Base analytics fact.

    ``kind`` is a stable discriminator (one of the ``KIND_*`` constants);
    ``ts_unix`` is captured at construction; ``data`` holds the typed
    subclass fields (auto-populated in :meth:`__post_init__`).
    """

    kind: str = ""
    ts_unix: float = field(default_factory=time.time)
    data: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Reflect typed subclass fields into self.data so `fact.data` and
        # `fact.to_dict()["data"]` agree. Manually-set `data` entries (if any)
        # win over the auto-reflected values, letting callers attach extras.
        extras: dict[str, Any] = {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name not in _BASE_FIELD_NAMES
        }
        if extras:
            self.data = {**extras, **self.data}
        # Defensive: ts_unix must always be a real timestamp.
        if not self.ts_unix:
            self.ts_unix = time.time()

    def to_dict(self) -> dict[str, Any]:
        """Serializable form: ``{"kind": ..., "ts_unix": ..., "data": {...}}``."""
        return {
            "kind": self.kind,
            "ts_unix": self.ts_unix,
            "data": dict(self.data),
        }


# --------------------------------------------------------------------------- #
# Concrete facts — one per event family.
# --------------------------------------------------------------------------- #
@dataclass
class TurnProfileFact(Fact):
    """How a turn was dispatched: which model/mode/profile and its outcome."""

    kind: str = KIND_TURN_PROFILE
    model: str = ""
    mode: str = ""
    profile: str = ""
    status: str = ""


@dataclass
class TurnTokenUsageFact(Fact):
    """Token accounting for a completed model call within a turn."""

    kind: str = KIND_TURN_TOKENS
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class CompactionFact(Fact):
    """A context-compaction event: phase, trigger, and token delta."""

    kind: str = KIND_COMPACTION
    phase: str = ""
    reason: str = ""
    strategy: str = ""
    status: str = ""
    before_tokens: int = 0
    after_tokens: int = 0


@dataclass
class GuardianReviewFact(Fact):
    """One Guardian (LLM-as-judge) verdict on a planned action."""

    kind: str = KIND_GUARDIAN_REVIEW
    tool: str = ""
    risk_level: str = ""
    outcome: str = ""  # allow | deny
    failure_reason: str = ""


@dataclass
class SkillInvocationFact(Fact):
    """A skill was invoked, explicitly (mention) or implicitly."""

    kind: str = KIND_SKILL_INVOKE
    skill_name: str = ""
    triggered_by: str = ""  # mention | implicit


@dataclass
class HookRunFact(Fact):
    """A lifecycle hook handler ran (or failed)."""

    kind: str = KIND_HOOK_RUN
    event: str = ""
    handler: str = ""
    outcome: str = ""  # ok | error | skipped
    duration_ms: float = 0.0


@dataclass
class GoalEventFact(Fact):
    """A goal lifecycle event.

    NOTE: the spec signature is ``(kind, goal)`` where ``kind`` is the goal
    event (set/updated/cleared). That name collides with the base ``Fact.kind``
    discriminator, so the goal-event field is renamed to ``event``; the fact
    discriminator stays the stable ``goal.event``.
    """

    kind: str = KIND_GOAL_EVENT
    event: str = ""  # set | updated | cleared
    goal: str = ""


@dataclass
class TurnSteerFact(Fact):
    """A mid-turn steer instruction was issued and accepted/rejected."""

    kind: str = KIND_TURN_STEER
    instruction: str = ""
    accepted: bool = False


@dataclass
class SubagentStartedFact(Fact):
    """A subagent was spawned from a parent agent path."""

    kind: str = KIND_SUBAGENT_STARTED
    parent_path: str = ""
    child_path: str = ""
    task_name: str = ""


# --------------------------------------------------------------------------- #
# Recorder
# --------------------------------------------------------------------------- #
Sink = Callable[[dict[str, Any]], None]


class AnalyticsRecorder:
    """Collects facts, drains them on demand, and fans each out to a sink.

    The recorder ALWAYS keeps its own in-memory buffer (drained via
    :meth:`drain`) so callers can batch-emit at turn end regardless of the
    sink. The ``sink`` is an additional side-effect per fact — by default it
    logs via :mod:`logging`; pass a callable ``sink(fact_dict)`` to ship facts
    to telemetry/OTel/etc. The sink is invoked OUTSIDE the internal lock so a
    slow or re-entrant sink cannot deadlock producers.
    """

    def __init__(self, sink: Sink | None = None) -> None:
        self._lock = threading.Lock()
        self._facts: list[dict[str, Any]] = []
        self._sink: Sink | None = sink

    def record(self, fact: Fact) -> None:
        """Serialize + buffer ``fact`` and fan it out to the sink."""
        payload = fact.to_dict()
        with self._lock:
            self._facts.append(payload)
        if self._sink is not None:
            self._sink(payload)
        else:
            logger.info(
                "analytics fact kind=%s data=%s",
                payload.get("kind"),
                payload.get("data"),
            )

    def drain(self) -> list[dict[str, Any]]:
        """Return all buffered fact dicts (in record order) and clear the buffer."""
        with self._lock:
            drained = list(self._facts)
            self._facts.clear()
        return drained

    def __len__(self) -> int:
        with self._lock:
            return len(self._facts)
