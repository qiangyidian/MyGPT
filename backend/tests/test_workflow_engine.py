"""Task 6: planner-executor-verifier workflow engine — RED tests.

Exercises the durable plan->execute->verify->(replan) state machine with
injected stubs (deterministic, no model calls). Covers:

  * plan validation (topological order, cycle rejection, missing-dep rejection,
    duplicate-id rejection)
  * dependency ordering (a step never runs before its dependencies)
  * parallelism (independent ready steps run concurrently -> max_concurrency)
  * retry (transient errors retried up to the policy; permanent errors fail)
  * verification (pass -> completed; fail -> failed)
  * templates over the same engine mirror the existing graph topology
  * the AttemptRepository persists per-step attempts (status + attempt_number)
  * durable step.* events are emitted when persistence is wired
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.agents.workflow.attempts import AttemptRepository
from app.agents.workflow.engine import WorkflowEngine
from app.agents.workflow.executor import RecordingExecutor
from app.agents.workflow.planner import (
    build_debate_plan,
    build_deep_research_plan,
    build_parallel_research_plan,
    build_plan_for_profile,
    validate_plan,
)
from app.agents.workflow.schemas import (
    Plan,
    PlanValidationError,
    RetryPolicy,
    Step,
    StepError,
    StepObservation,
    VerificationVerdict,
    VerifierResult,
)
from app.agents.workflow.verifier import RuleBasedVerifier, ScriptedVerifier, Verifier
from app.models import AgentAttempt, AgentRun, Conversation, Message


# --------------------------------------------------------------------------- #
# Plan construction helpers + stub executors / verifiers
# --------------------------------------------------------------------------- #
def _step(
    sid: str,
    *,
    deps: list[str] | None = None,
    retries: int = 0,
    transient: tuple[str, ...] = (),
    criteria: dict[str, Any] | None = None,
) -> Step:
    return Step(
        id=sid,
        dependencies=list(deps or []),
        retry_policy=RetryPolicy(max_retries=retries, transient_errors=transient),
        acceptance_criteria=criteria or {},
    )


def _seq_plan(*ids: str) -> Plan:
    """A purely sequential plan: ids[0] -> ids[1] -> ... (each depends on prior)."""
    steps = []
    for i, sid in enumerate(ids):
        steps.append(_step(sid, deps=([ids[i - 1]] if i > 0 else [])))
    return Plan(goal="seq", steps=steps, max_replans=0)


class FailingExecutor:
    """Stub that raises a transient error N times then succeeds, or always
    raises a permanent error."""

    def __init__(
        self,
        *,
        transient_times: dict[str, int] | None = None,
        permanent: set[str] | None = None,
    ) -> None:
        self.transient_times = transient_times or {}
        self.permanent = permanent or set()
        self.calls: dict[str, int] = {}

    async def execute(
        self, step: Step, upstream: dict[str, StepObservation]
    ) -> StepObservation:
        n = self.calls.get(step.id, 0) + 1
        self.calls[step.id] = n
        if step.id in self.permanent:
            raise StepError(f"permanent boom in {step.id}", transient=False)
        need = self.transient_times.get(step.id, 0)
        if n <= need:
            raise StepError(f"transient flake {n}/{need} in {step.id}", transient=True)
        return StepObservation(step_id=step.id, output=f"[{step.id}] ok@{n}")


# --------------------------------------------------------------------------- #
# Plan validation
# --------------------------------------------------------------------------- #
def test_plan_with_cycle_rejected_at_build_time():
    plan = Plan(
        goal="cyclic",
        steps=[_step("a", deps=["b"]), _step("b", deps=["a"])],
    )
    with pytest.raises(PlanValidationError):
        validate_plan(plan)


def test_plan_with_self_cycle_rejected():
    plan = Plan(goal="self", steps=[_step("a", deps=["a"])])
    with pytest.raises(PlanValidationError):
        validate_plan(plan)


def test_plan_with_missing_dependency_rejected():
    plan = Plan(goal="dangling", steps=[_step("a", deps=["nope"])])
    with pytest.raises(PlanValidationError):
        validate_plan(plan)


def test_plan_with_duplicate_step_ids_rejected():
    plan = Plan(goal="dup", steps=[_step("a"), _step("a")])
    with pytest.raises(PlanValidationError):
        validate_plan(plan)


def test_valid_plan_topological_order():
    # a -> b -> c, plus d parallel to b (depends only on a)
    plan = Plan(
        goal="ok",
        steps=[
            _step("a"), _step("b", deps=["a"]), _step("c", deps=["b"]),
            _step("d", deps=["a"]),
        ],
    )
    validate_plan(plan)  # no raise
    order = plan.topological_order()
    # a before b and d; b before c
    assert order.index("a") < order.index("b")
    assert order.index("a") < order.index("d")
    assert order.index("b") < order.index("c")


# --------------------------------------------------------------------------- #
# Dependency ordering + parallelism (no DB, pure engine logic)
# --------------------------------------------------------------------------- #
async def test_step_never_runs_before_its_dependencies():
    plan = _seq_plan("a", "b", "c")
    exec_ = RecordingExecutor()
    engine = WorkflowEngine(executor=exec_)
    result = await engine.run(plan)
    assert result.status == "completed"
    # Strictly sequential start order.
    assert exec_.started_order == ["a", "b", "c"]


async def test_independent_ready_steps_run_in_parallel():
    plan = Plan(
        goal="parallel",
        steps=[_step("research_a"), _step("research_b")],
        max_replans=0,
    )
    exec_ = RecordingExecutor()
    engine = WorkflowEngine(executor=exec_)
    result = await engine.run(plan)
    assert result.status == "completed"
    assert result.max_concurrency == 2


async def test_sequential_plan_observes_concurrency_one():
    plan = _seq_plan("a", "b", "c")
    engine = WorkflowEngine(executor=RecordingExecutor())
    result = await engine.run(plan)
    assert result.max_concurrency == 1


async def test_join_runs_after_both_predecessors():
    # two independent producers, then a consumer that joins on both.
    plan = Plan(
        goal="join",
        steps=[
            _step("p1"), _step("p2"),
            _step("join", deps=["p1", "p2"]),
        ],
        max_replans=0,
    )
    exec_ = RecordingExecutor()
    engine = WorkflowEngine(executor=exec_)
    result = await engine.run(plan)
    assert result.status == "completed"
    # peak concurrency is the two producers running together.
    assert result.max_concurrency == 2
    # the join ran AFTER both producers.
    assert exec_.started_order[-1] == "join"


async def test_upstream_observations_visible_to_downstream():
    plan = _seq_plan("a", "b")
    seen: dict[str, dict[str, StepObservation]] = {}

    class _Capture(RecordingExecutor):
        async def execute(self, step, upstream):
            seen[step.id] = dict(upstream)
            return await super().execute(step, upstream)

    engine = WorkflowEngine(executor=_Capture(outputs={"a": "AAA"}))
    result = await engine.run(plan)
    assert result.status == "completed"
    # step b saw a's checkpointed observation.
    assert "a" in seen["b"]
    assert seen["b"]["a"].output == "AAA"


# --------------------------------------------------------------------------- #
# Retry policy (transient vs permanent)
# --------------------------------------------------------------------------- #
async def test_transient_error_is_retried_up_to_policy():
    plan = Plan(
        goal="retry",
        steps=[_step("flaky", retries=2, transient=("transient",))],
        max_replans=0,
    )
    exec_ = FailingExecutor(transient_times={"flaky": 2})  # fail twice, then pass
    engine = WorkflowEngine(executor=exec_)
    result = await engine.run(plan)
    assert result.status == "completed"
    assert exec_.calls["flaky"] == 3  # 2 retries + 1 success
    assert result.observations["flaky"].output == "[flaky] ok@3"


async def test_exhausting_retries_does_not_succeed():
    plan = Plan(
        goal="retry-exhaust",
        steps=[_step("flaky", retries=1, transient=("transient",))],
        max_replans=0,
    )
    exec_ = FailingExecutor(transient_times={"flaky": 99})  # always transient
    engine = WorkflowEngine(executor=exec_)
    result = await engine.run(plan)
    assert result.status == "failed"


async def test_permanent_error_fails_step_immediately():
    plan = Plan(
        goal="perm",
        steps=[_step("bad", retries=5, transient=("transient",))],
        max_replans=0,
    )
    exec_ = FailingExecutor(permanent={"bad"})
    engine = WorkflowEngine(executor=exec_)
    result = await engine.run(plan)
    assert result.status == "failed"
    # permanent error => no retries consumed.
    assert exec_.calls["bad"] == 1


# --------------------------------------------------------------------------- #
# Verification (pass / fail)
# --------------------------------------------------------------------------- #
async def test_pass_verdict_completes():
    plan = _seq_plan("a", "b")
    engine = WorkflowEngine(
        executor=RecordingExecutor(),
        verifier=ScriptedVerifier([VerifierResult(verdict=VerificationVerdict.pass_)]),
    )
    result = await engine.run(plan)
    assert result.status == "completed"


async def test_fail_verdict_terminates_failed():
    plan = _seq_plan("a")
    engine = WorkflowEngine(
        executor=RecordingExecutor(),
        verifier=ScriptedVerifier([VerifierResult(verdict=VerificationVerdict.fail)]),
        # max_replans irrelevant for a hard fail verdict
    )
    result = await engine.run(plan)
    assert result.status == "failed"


async def test_rule_based_verifier_checks_acceptance_criteria():
    # step 'a' has a min-length criterion its output meets; 'b' does not.
    plan = Plan(
        goal="criteria",
        steps=[
            Step(id="a", acceptance_criteria={"min_chars": 3}),
            Step(id="b", acceptance_criteria={"min_chars": 100}),
        ],
        max_replans=0,
    )
    v = RuleBasedVerifier()
    obs = {
        "a": StepObservation(step_id="a", output="ok"),
        "b": StepObservation(step_id="b", output="too short"),
    }
    res = await v.verify(plan, obs)
    # b failed the criterion -> revise pointing at b.
    assert res.verdict == VerificationVerdict.revise
    assert "b" in res.revise_step_ids


# --------------------------------------------------------------------------- #
# Templates mirror existing graph topology
# --------------------------------------------------------------------------- #
def test_deep_research_template_is_sequential_chain():
    p = build_deep_research_plan("compare A and B")
    validate_plan(p)
    ids = p.step_ids
    assert ids == ["researcher", "analyst", "writer"]
    assert p.get("analyst").dependencies == ["researcher"]
    assert p.get("writer").dependencies == ["analyst"]


def test_parallel_research_template_has_two_parallel_researchers():
    p = build_parallel_research_plan("vector dbs")
    validate_plan(p)
    # web + kb researchers share a single dependency (coordinator) and have no
    # inter-dependency -> they are in the ready set together.
    web = p.get("web-researcher")
    kb = p.get("kb-researcher")
    assert web.dependencies == ["coordinator"]
    assert kb.dependencies == ["coordinator"]
    assert set(p.get("analyst").dependencies) == {"web-researcher", "kb-researcher"}


def test_debate_template_advocates_parallel_judge_joins():
    p = build_debate_plan("Python vs Go")
    validate_plan(p)
    advs = [p.get("advocate-a"), p.get("advocate-b")]
    assert all(a.dependencies == [] for a in advs)
    assert set(p.get("judge").dependencies) == {"advocate-a", "advocate-b"}


def test_build_plan_for_profile_dispatches():
    for profile, builder in (
        ("deep_research", build_deep_research_plan),
        ("parallel_research", build_parallel_research_plan),
        ("debate", build_debate_plan),
    ):
        p = build_plan_for_profile(profile, "some question")
        assert isinstance(p, Plan)
        validate_plan(p)
        assert p.profile == profile


async def test_parallel_research_template_runs_with_concurrency_two():
    p = build_parallel_research_plan("x")
    engine = WorkflowEngine(executor=RecordingExecutor(), verifier=PassVerifier())
    result = await engine.run(p)
    assert result.status == "completed"
    # two researchers run together after the coordinator completes.
    assert result.max_concurrency == 2


# --------------------------------------------------------------------------- #
# AttemptRepository (real DB, in-memory SQLite)
# --------------------------------------------------------------------------- #
async def _seed_run(db_session) -> AgentRun:
    conv = Conversation(user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"), title="wf")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.flush()
    run = AgentRun(conversation_id=conv.id, message_id=msg.id, runtime="native", status="running")
    db_session.add(run)
    await db_session.commit()
    return run


async def test_attempt_repository_lifecycle(db_session):
    run = await _seed_run(db_session)
    repo = AttemptRepository(db_session)

    attempt = await repo.create_pending(run.id, "step-a", attempt_number=1)
    assert attempt.status == "pending"
    assert attempt.step_key == "step-a"
    assert attempt.attempt_number == 1

    await repo.mark_running(attempt)
    assert attempt.status == "running"
    assert attempt.started_at is not None

    await repo.mark_done(attempt, usage={"total_tokens": 42})
    assert attempt.status == "done"
    assert attempt.finished_at is not None
    assert (attempt.usage or {}).get("total_tokens") == 42


async def test_attempt_repository_next_attempt_number_increments(db_session):
    run = await _seed_run(db_session)
    repo = AttemptRepository(db_session)

    # First attempt for a step -> 1; after creating it, next -> 2.
    assert await repo.next_attempt_number(run.id, "s") == 1
    await repo.create_pending(run.id, "s", attempt_number=1)
    await db_session.commit()
    assert await repo.next_attempt_number(run.id, "s") == 2


async def test_attempt_repository_marks_error(db_session):
    run = await _seed_run(db_session)
    repo = AttemptRepository(db_session)
    attempt = await repo.create_pending(run.id, "s", attempt_number=1)
    await repo.mark_running(attempt)
    await repo.mark_error(attempt, "boom")
    assert attempt.status == "error"
    assert attempt.error == "boom"
    assert attempt.finished_at is not None


# --------------------------------------------------------------------------- #
# Persistence + events: a DB-backed engine run writes attempts + step.* events
# --------------------------------------------------------------------------- #
async def test_engine_run_persists_attempts_and_events(db_session):
    from tests.conftest import TestSessionLocal

    run = await _seed_run(db_session)
    plan = _seq_plan("a", "b")

    engine = WorkflowEngine(
        executor=RecordingExecutor(),
        verifier=PassVerifier(),
        run_id=run.id,
        session_factory=TestSessionLocal,
    )
    result = await engine.run(plan)
    assert result.status == "completed"

    # Each step wrote at least one AgentAttempt row that reached 'done'.
    rows = (
        await db_session.execute(
            select(AgentAttempt).where(AgentAttempt.run_id == run.id)
        )
    ).scalars().all()
    step_keys = {r.step_key for r in rows}
    assert {"a", "b"} <= step_keys
    assert all(r.status == "done" for r in rows if r.step_key in {"a", "b"})

    # Durable step.* events were appended to run_events.
    from app.models import RunEvent

    events = (
        await db_session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id)
        )
    ).scalars().all()
    types = {e.event_type for e in events}
    assert "step.started" in types
    assert "step.completed" in types


# --------------------------------------------------------------------------- #
# Convenience: a verifier that always passes (for engine tests that don't care)
# --------------------------------------------------------------------------- #
class PassVerifier(Verifier):
    async def verify(self, plan: Plan, observations: dict[str, StepObservation]) -> VerifierResult:
        return VerifierResult(verdict=VerificationVerdict.pass_)
