"""Live, mutable plan with Codex-style status discipline (the ``update_plan`` tool).

Codex exposes ``update_plan`` as a real tool the model drives: a step list where
exactly one step is ``in_progress`` at a time, items can't jump ``pending →
completed`` (they must go ``in_progress`` first), and the plan updates as
understanding changes. This module is the pure state machine behind that tool —
the runtime registers a thin tool wrapper around :class:`PlanState` and emits
``plan_updated`` events (wired in integration).

Kept separate from the runtime so the discipline is unit-testable with no LLM.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

StepStatus = Literal["pending", "in_progress", "completed"]
_VALID = {"pending", "in_progress", "completed"}


class PlanError(ValueError):
    """Raised when a plan transition violates the status discipline."""


@dataclass
class PlanStep:
    id: str
    title: str
    status: StepStatus = "pending"


@dataclass
class PlanState:
    """A mutable plan enforcing Codex's status discipline."""

    steps: list[PlanStep] = field(default_factory=list)

    def replace(self, steps: Iterable[PlanStep]) -> list[PlanStep]:
        """Swap in a new step list (a pivot). Validates ≤1 in_progress."""
        new = list(steps)
        for s in new:
            if s.status not in _VALID:
                raise PlanError(f"invalid status {s.status!r} on step {s.id}")
        self._enforce_single_in_progress(new)
        self.steps = new
        return list(self.steps)

    def transition(self, step_id: str, status: StepStatus) -> PlanStep:
        """Move a step to ``status``, enforcing the discipline.

        Rules:
          * ``pending → completed`` is forbidden (must pass through in_progress).
          * at most one ``in_progress`` at a time — transitioning a *different*
            step to in_progress while another is in_progress raises.
        """
        if status not in _VALID:
            raise PlanError(f"invalid status {status!r}")
        step = self._find(step_id)
        if status == "completed" and step.status == "pending":
            raise PlanError(
                f"step {step_id} cannot jump pending → completed; set in_progress first"
            )
        if status == "in_progress":
            for s in self.steps:
                if s.id != step_id and s.status == "in_progress":
                    raise PlanError(
                        f"another step ({s.id}) is in_progress; complete it before starting {step_id}"
                    )
        step.status = status
        return step

    def has_in_progress(self) -> bool:
        return any(s.status == "in_progress" for s in self.steps)

    def all_completed(self) -> bool:
        return bool(self.steps) and all(s.status == "completed" for s in self.steps)

    def to_public(self) -> list[dict]:
        return [{"id": s.id, "title": s.title, "status": s.status} for s in self.steps]

    # -- internals --
    def _find(self, step_id: str) -> PlanStep:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise PlanError(f"unknown step id: {step_id}")

    @staticmethod
    def _enforce_single_in_progress(steps: list[PlanStep]) -> None:
        in_prog = [s for s in steps if s.status == "in_progress"]
        if len(in_prog) > 1:
            raise PlanError(
                f"at most one step may be in_progress; got {len(in_prog)}: "
                + ", ".join(s.id for s in in_prog)
            )
