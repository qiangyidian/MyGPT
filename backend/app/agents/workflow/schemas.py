"""Typed workflow schemas for the planner-executor-verifier engine (Task 6).

These generalize the existing static :mod:`~app.agents.graph` model into a
verifiable plan -> execute -> verify -> replan state machine. A :class:`Plan`
is a versioned DAG of :class:`Step` objects; each step declares its
``dependencies`` (the generalization of ``AgentGraph.predecessors``), a retry
policy, and acceptance criteria the verifier checks.

The schemas are deliberately framework-light (pydantic v2 ``BaseModel`` to
match the rest of the codebase) and carry NO execution behavior: the engine
(:mod:`~app.agents.workflow.engine`) drives them, the planner
(:mod:`~app.agents.workflow.planner`) constructs + revises them, and the
executor / verifier (:mod:`~app.agents.workflow.executor`,
:mod:`~app.agents.workflow.verifier`) operate on them.

Validation lives on the schema (not the constructor) so a plan can be built up
incrementally and checked explicitly via :func:`~app.agents.workflow.planner.validate_plan`
(the engine also validates before running).
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, ConfigDict


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
class VerificationVerdict(str, Enum):
    """Outcome of verifying a plan's accumulated observations.

    * ``pass_``   — the plan's goal is met; the run is ``completed``.
    * ``revise``  — some steps' output failed acceptance; the planner produces a
      NEW versioned plan that reworks only the flagged steps (consuming one
      replan unit).
    * ``fail``    — an unrecoverable verification failure; the run is ``failed``
      regardless of remaining replan budget.

    (The trailing underscore on ``pass_`` avoids shadowing the Python keyword;
    its serialization value is the bare ``"pass"``.)
    """

    pass_ = "pass"
    revise = "revise"
    fail = "fail"


class VerifierResult(BaseModel):
    """What a :class:`~app.agents.workflow.verifier.Verifier` returns."""

    verdict: VerificationVerdict
    # Free-form findings (human-readable) explaining the verdict.
    findings: list[str] = Field(default_factory=list)
    # Step ids that must be reworked on a ``revise``. The planner retains every
    # other completed step and only re-runs these.
    revise_step_ids: list[str] = Field(default_factory=list)
    note: str = ""


# --------------------------------------------------------------------------- #
# Retry policy + errors
# --------------------------------------------------------------------------- #
class RetryPolicy(BaseModel):
    """Per-step retry configuration.

    ``transient_errors`` is a list of substrings; if any appears in an error
    message the failure is classified transient and retried (up to
    ``max_retries``). A :class:`StepError` carrying ``transient=True`` is also
    treated as transient regardless of the message. Anything else is permanent
    and fails the step immediately.
    """

    max_retries: int = 0
    transient_errors: list[str] = Field(default_factory=list)

    def is_transient(self, error: BaseException | str) -> bool:
        if isinstance(error, StepError) and error.transient:
            return True
        msg = str(error)
        return any(token in msg for token in self.transient_errors)


class StepError(Exception):
    """Raised by a :class:`~app.agents.workflow.executor.StepExecutor` to signal
    a step failure with a transient/permanent classification.

    The engine inspects :attr:`transient` (and the step's
    :class:`RetryPolicy`) to decide whether to retry. Plain exceptions from an
    executor are treated as permanent by default (fail-fast) so a buggy stub
    can't accidentally trigger infinite retries.
    """

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


# --------------------------------------------------------------------------- #
# Plan + Step
# --------------------------------------------------------------------------- #
class Step(BaseModel):
    """One node in the workflow DAG.

    Mirrors ``AgentGraphNode`` (id/role/stage) plus the execution metadata the
    engine needs: ``dependencies`` (= ``AgentGraph.predecessors``), a
    :class:`RetryPolicy`, ``acceptance_criteria`` the verifier evaluates, and a
    ``cost_estimate`` for budgeting. ``skip`` marks a step whose observation is
    already carried over from a prior plan version (a revised plan retains
    completed valid work); the engine does not re-execute skipped steps.
    """

    model_config = ConfigDict(arbitrary_types_allowed=False)

    id: str
    dependencies: list[str] = Field(default_factory=list)
    role: str = ""
    name: str = ""
    model: str = ""
    task_description: str = ""
    tool_allowlist: list[str] = Field(default_factory=list)
    timeout_seconds: float | None = None
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    acceptance_criteria: dict[str, Any] = Field(default_factory=dict)
    cost_estimate: float = 0.0
    # Carried over (already done) in a revised plan -> do not re-execute.
    skip: bool = False


class StepObservation(BaseModel):
    """The checkpointed output of one step.

    Persisted in-memory across the run and exposed to downstream steps (via the
    executor's ``upstream`` argument) and to the verifier. ``attempts`` counts
    the underlying executions (1 + retries) so cost accounting is observable.
    """

    step_id: str
    output: str = ""
    structured: Any | None = None
    usage: dict[str, Any] | None = None
    attempts: int = 1
    status: str = "done"  # done | error


class PlanValidationError(Exception):
    """Raised when a plan has a cycle, a missing dependency, or duplicate ids."""


class Plan(BaseModel):
    """A versioned, validated DAG of :class:`Step` objects.

    ``version`` + ``replan_count`` increase together on each revision; the
    engine refuses to revise past ``max_replans``. ``carry_observations`` holds
    outputs from prior versions that the new version retains (skipped steps) so
    the engine does not recompute them.
    """

    version: int = 1
    goal: str = ""
    profile: str = ""
    steps: list[Step] = Field(default_factory=list)
    replan_count: int = 0
    max_replans: int = 1
    carry_observations: dict[str, StepObservation] = Field(default_factory=dict)

    # ---- lookups ----------------------------------------------------------
    def get(self, step_id: str) -> Step:
        """Return the step with ``step_id``. Raises ``KeyError`` if absent."""
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    @property
    def step_ids(self) -> list[str]:
        return [s.id for s in self.steps]

    def topological_order(self) -> list[str]:
        """Kahn's algorithm. Raises :class:`PlanValidationError` on a cycle."""
        return _toposort(self)

    def validate(self) -> None:
        """Validate uniqueness, dependency presence, and acyclicity."""
        _validate(self)


# --------------------------------------------------------------------------- #
# Workflow result
# --------------------------------------------------------------------------- #
class WorkflowResult(BaseModel):
    """Terminal outcome of :meth:`WorkflowEngine.run`."""

    status: str  # completed | failed
    replans: int = 0
    max_concurrency: int = 0
    observations: dict[str, StepObservation] = Field(default_factory=dict)
    findings: list[str] = Field(default_factory=list)
    verifier_results: list[VerifierResult] = Field(default_factory=list)
    error: str | None = None


# --------------------------------------------------------------------------- #
# Internal: topological sort + validation (shared with the planner)
# --------------------------------------------------------------------------- #
def _toposort(plan: Plan) -> list[str]:
    deps = {s.id: list(s.dependencies) for s in plan.steps}
    order: list[str] = []
    # Repeatedly take nodes whose deps are all already emitted.
    remaining = dict(deps)
    while remaining:
        ready = [sid for sid, d in remaining.items() if all(x in order for x in d)]
        if not ready:
            raise PlanValidationError(
                f"cycle detected in plan (involving {sorted(remaining)})"
            )
        # Stable: preserve declared step order among simultaneously-ready nodes.
        ready_in_order = [sid for sid in plan.step_ids if sid in ready]
        for sid in ready_in_order:
            order.append(sid)
            del remaining[sid]
    return order


def _validate(plan: Plan) -> None:
    ids = [s.id for s in plan.steps]
    # Duplicate ids.
    seen: set[str] = set()
    for sid in ids:
        if sid in seen:
            raise PlanValidationError(f"duplicate step id: {sid!r}")
        seen.add(sid)
    # Missing dependencies.
    id_set = set(ids)
    for s in plan.steps:
        for d in s.dependencies:
            if d not in id_set:
                raise PlanValidationError(
                    f"step {s.id!r} depends on unknown step {d!r}"
                )
    # Cycles (incl. self-loops).
    _toposort(plan)
