"""Structured verification for the workflow engine (Task 6).

A :class:`Verifier` inspects the plan's accumulated observations and returns a
:class:`~app.agents.workflow.schemas.VerifierResult` whose verdict is one of
``pass`` / ``revise`` / ``fail``. ``revise`` consumes one replan unit and the
planner produces a NEW versioned plan; ``pass`` completes the run; ``fail``
terminates it.

This module ships:

  * :class:`Verifier` — the :class:`typing.Protocol` the engine consumes.
  * :class:`RuleBasedVerifier` — a deterministic verifier that evaluates each
    step's ``acceptance_criteria`` (e.g. ``min_chars``) against its observation.
  * :class:`ScriptedVerifier` — a test double that replays a scripted sequence
    of verdicts (so the engine's replan loop is fully exercisable without an
    LLM).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.agents.workflow.schemas import (
    Plan,
    StepObservation,
    VerificationVerdict,
    VerifierResult,
)


@runtime_checkable
class Verifier(Protocol):
    """Inspect a plan + its observations and return a verdict."""

    async def verify(
        self,
        plan: Plan,
        observations: dict[str, StepObservation],
    ) -> VerifierResult: ...


class RuleBasedVerifier:
    """Evaluate each step's ``acceptance_criteria``.

    Supported criteria (extend cautiously — keep it deterministic):

      * ``min_chars`` — the observation's ``output`` must be at least this many
        characters long.

    If every step passes its criteria (or has none), the verdict is ``pass``.
    Otherwise the verdict is ``revise`` with the failing step ids. The
    rule-based verifier never returns ``fail`` — that is reserved for explicit
    (e.g. model-driven or scripted) hard-fail judgements.
    """

    async def verify(
        self, plan: Plan, observations: dict[str, StepObservation]
    ) -> VerifierResult:
        failing: list[str] = []
        findings: list[str] = []
        for step in plan.steps:
            if step.skip:
                # Carried-over (already-accepted) step — don't re-litigate.
                continue
            crit = step.acceptance_criteria or {}
            obs = observations.get(step.id)
            if obs is None or obs.status != "done":
                failing.append(step.id)
                findings.append(f"{step.id}: no completed observation")
                continue
            min_chars = crit.get("min_chars")
            if isinstance(min_chars, int) and len(obs.output or "") < min_chars:
                failing.append(step.id)
                findings.append(
                    f"{step.id}: output too short ({len(obs.output or '')} < {min_chars})"
                )
        if not failing:
            return VerifierResult(verdict=VerificationVerdict.pass_, findings=findings)
        return VerifierResult(
            verdict=VerificationVerdict.revise,
            findings=findings,
            revise_step_ids=failing,
        )


class ScriptedVerifier:
    """Test double: replay a fixed sequence of verdicts.

    Items may be :class:`VerifierResult` objects or bare verdict strings
    (``"pass"``/``"revise"``/``"fail"``). When a string verdict is ``"revise"``,
    ``revise_step_ids`` (provided at construction or via :meth:`with_rework`)
    names the steps the planner should rework. The verifier returns results in
    order; if the engine asks beyond the end, the last entry is repeated (so a
    short script can stand in for "keep revising").
    """

    def __init__(
        self,
        results: list,
        *,
        revise_step_ids: list[str] | None = None,
    ) -> None:
        self._raw = list(results)
        self._revise_step_ids = list(revise_step_ids or [])
        self._index = 0

    async def verify(
        self, plan: Plan, observations: dict[str, StepObservation]
    ) -> VerifierResult:
        idx = min(self._index, len(self._raw) - 1)
        self._index += 1
        item = self._raw[idx]
        if isinstance(item, VerifierResult):
            return item
        verdict = _coerce_verdict(item)
        return VerifierResult(
            verdict=verdict,
            revise_step_ids=(
                list(self._revise_step_ids)
                if verdict == VerificationVerdict.revise
                else []
            ),
        )


def _coerce_verdict(value: str) -> VerificationVerdict:
    v = (value or "").strip().lower().rstrip("_")
    # accept "pass" / "pass_"
    if v in ("pass", "pass_", "ok", "done"):
        return VerificationVerdict.pass_
    if v in ("revise", "rework", "retry"):
        return VerificationVerdict.revise
    if v in ("fail", "failed", "error"):
        return VerificationVerdict.fail
    raise ValueError(f"unknown verdict string: {value!r}")
