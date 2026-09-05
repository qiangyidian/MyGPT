"""Task 6: bounded replanning — RED tests.

A ``revise`` verdict consumes one replan unit and the planner produces a NEW
versioned plan that RETAINS completed valid work (steps whose observations
passed) and reworks only the flagged steps. Exhausting ``max_replans`` -> the
run terminates ``failed``.
"""
from __future__ import annotations

import asyncio

import pytest

from app.agents.workflow.engine import WorkflowEngine
from app.agents.workflow.planner import revise_plan
from app.agents.workflow.schemas import (
    Plan,
    PlanValidationError,
    Step,
    StepObservation,
    VerificationVerdict,
    VerifierResult,
)
from app.agents.workflow.verifier import ScriptedVerifier


# --------------------------------------------------------------------------- #
# Local helpers (mirror the ones in test_workflow_engine.py to keep this file
# standalone).
# --------------------------------------------------------------------------- #
def _step(sid: str, deps: list[str] | None = None) -> Step:
    return Step(id=sid, dependencies=list(deps or []))


def _seq_plan(*ids: str, max_replans: int = 1) -> Plan:
    steps = []
    for i, sid in enumerate(ids):
        steps.append(_step(sid, deps=([ids[i - 1]] if i > 0 else [])))
    return Plan(goal="seq", steps=steps, max_replans=max_replans)


class _RecExecutor:
    def __init__(self, outputs: dict[str, str] | None = None) -> None:
        self.outputs = outputs or {}
        self.ran: list[str] = []

    async def execute(self, step: Step, upstream: dict[str, StepObservation]) -> StepObservation:
        self.ran.append(step.id)
        await asyncio.sleep(0)
        return StepObservation(step_id=step.id, output=self.outputs.get(step.id, f"[{step.id}]"))


# --------------------------------------------------------------------------- #
# Replan within budget
# --------------------------------------------------------------------------- #
async def test_failed_verification_replans_within_budget():
    """First verify -> revise; second verify -> pass. Exactly one replan."""
    plan = _seq_plan("a", "b", max_replans=2)
    engine = WorkflowEngine(
        executor=_RecExecutor(),
        verifier=ScriptedVerifier([
            VerifierResult(verdict=VerificationVerdict.revise, revise_step_ids=["b"]),
            VerifierResult(verdict=VerificationVerdict.pass_),
        ]),
    )
    result = await engine.run(plan)
    assert result.replans == 1
    assert result.status == "completed"


async def test_revise_then_pass_with_verifier_results_kwarg():
    """The engine.run convenience ``verifier_results=[...]`` wires a scripted
    verifier in one call, matching the spec example."""
    plan = _seq_plan("a", max_replans=2)
    engine = WorkflowEngine(executor=_RecExecutor())
    result = await engine.run(
        plan, verifier_results=["revise", "pass"],
        revise_step_ids=["a"],
    )
    assert result.replans == 1
    assert result.status == "completed"


# --------------------------------------------------------------------------- #
# Exhausting the budget
# --------------------------------------------------------------------------- #
async def test_exhausting_replan_budget_terminates_failed():
    """The verifier keeps saying revise but the plan allows only 1 replan."""
    plan = _seq_plan("a", max_replans=1)
    engine = WorkflowEngine(
        executor=_RecExecutor(),
        verifier=ScriptedVerifier([
            VerifierResult(verdict=VerificationVerdict.revise, revise_step_ids=["a"]),
            VerifierResult(verdict=VerificationVerdict.revise, revise_step_ids=["a"]),
            VerifierResult(verdict=VerificationVerdict.revise, revise_step_ids=["a"]),
        ]),
    )
    result = await engine.run(plan)
    assert result.status == "failed"
    # we attempted one revision (replan_count went 0 -> 1) before giving up.
    assert result.replans == 1


async def test_zero_replans_revise_terminates_failed():
    """max_replans=0: any revise verdict immediately fails."""
    plan = _seq_plan("a", max_replans=0)
    engine = WorkflowEngine(
        executor=_RecExecutor(),
        verifier=ScriptedVerifier([
            VerifierResult(verdict=VerificationVerdict.revise, revise_step_ids=["a"]),
        ]),
    )
    result = await engine.run(plan)
    assert result.status == "failed"
    assert result.replans == 0


# --------------------------------------------------------------------------- #
# Replan retains completed valid work
# --------------------------------------------------------------------------- #
def test_revise_plan_rejects_unknown_step_ids():
    # A buggy/scripted verifier returning an unknown revise id must fail loudly
    # instead of silently marking every step skip and looping to max_replans.
    plan = _seq_plan("a", "b")
    with pytest.raises(PlanValidationError):
        revise_plan(plan, revise_step_ids=["nonexistent"], observations={})


def test_revise_plan_retains_done_steps_and_reworks_flagged():
    plan = _seq_plan("a", "b", "c")
    observations = {
        "a": StepObservation(step_id="a", output="good"),
        "b": StepObservation(step_id="b", output="bad"),
        "c": StepObservation(step_id="c", output="good"),
    }
    revised = revise_plan(plan, revise_step_ids=["b"], observations=observations)
    # version bumped; replan_count incremented; flagged step re-runs, others kept.
    assert revised.version == plan.version + 1
    assert revised.replan_count == plan.replan_count + 1
    assert revised.get("a").skip is True          # observation retained
    assert revised.get("c").skip is True          # observation retained
    assert revised.get("b").skip is False         # flagged -> re-run
    # carried-over observations are exposed so the engine does not recompute them.
    assert revised.carry_observations["a"].output == "good"
    assert revised.carry_observations["c"].output == "good"
    assert "b" not in revised.carry_observations


async def test_engine_replan_only_re_runs_flagged_step():
    """When verify flags only 'b', the revision re-runs 'b' but NOT the
    already-passing 'a'/'c'. A real executor with observable side-effects
    proves no work was redone."""
    plan = _seq_plan("a", "b", "c", max_replans=2)
    runs: list[str] = []

    class _Tracking:
        async def execute(self, step: Step, upstream: dict[str, StepObservation]) -> StepObservation:
            runs.append(step.id)
            await asyncio.sleep(0)
            return StepObservation(step_id=step.id, output=f"[{step.id}]")

    engine = WorkflowEngine(
        executor=_Tracking(),
        verifier=ScriptedVerifier([
            VerifierResult(verdict=VerificationVerdict.revise, revise_step_ids=["b"]),
            VerifierResult(verdict=VerificationVerdict.pass_),
        ]),
    )
    result = await engine.run(plan)
    assert result.status == "completed"
    assert result.replans == 1
    # First pass ran a, b, c. Revision re-ran ONLY b. a and c were retained.
    assert runs.count("a") == 1
    assert runs.count("c") == 1
    assert runs.count("b") == 2  # original + one re-run after revise


async def test_revised_plan_still_validates():
    """A revised plan must still be a valid DAG (no cycles / dangling deps)."""
    plan = _seq_plan("a", "b", max_replans=2)
    revised = revise_plan(
        plan, revise_step_ids=["b"],
        observations={"a": StepObservation(step_id="a", output="x")},
    )
    # must not raise.
    from app.agents.workflow.planner import validate_plan

    validate_plan(revised)
