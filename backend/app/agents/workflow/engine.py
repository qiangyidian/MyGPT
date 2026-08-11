"""Planner-executor-verifier workflow engine (Task 6).

:class:`WorkflowEngine` orchestrates the durable plan -> execute -> verify ->
(bounded) replan state machine over an arbitrary :class:`~app.agents.workflow.schemas.Plan`.

Execution model
---------------
Each step is scheduled as an asyncio task that:

  1. waits for its dependencies' observations (an ``asyncio.Event`` per step),
  2. acquires a bounded-concurrency semaphore and increments the in-flight
     counter (the engine records the peak -> ``result.max_concurrency``),
  3. runs the injected :class:`~app.agents.workflow.executor.StepExecutor`,
  4. retries only on transient errors up to the step's :class:`RetryPolicy`,
  5. checkpoints its observation so downstream steps (and the verifier) see it.

After every step completes, the :class:`~app.agents.workflow.verifier.Verifier`
inspects the accumulated observations. ``pass`` completes the run; ``fail``
terminates it; ``revise`` consumes one replan unit and the planner produces a
NEW versioned plan that RETAINS completed valid work and reworks only the
flagged steps. Exhausting ``max_replans`` terminates ``failed``.

Persistence (best-effort, optional)
-----------------------------------
When ``run_id`` + ``session_factory`` are provided, each attempt (initial +
each retry) persists an :class:`~app.models.AgentAttempt` row through the
:class:`~app.agents.workflow.attempts.AttemptRepository` and emits durable
``step.started`` / ``step.completed`` / ``step.failed`` / ``plan.revised``
events via :func:`~app.agents.events.append_event_safe`. Persistence NEVER
breaks the run: a DB failure is swallowed (best-effort). When those are
``None`` the engine runs in-memory, which is what makes the core logic fully
unit-testable with stubs.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from app.agents.events import append_event_safe
from app.agents.workflow.attempts import AttemptRepository
from app.agents.workflow.planner import revise_plan, validate_plan
from app.agents.workflow.schemas import (
    Plan,
    Step,
    StepError,
    StepObservation,
    VerificationVerdict,
    VerifierResult,
    WorkflowResult,
)
from app.agents.workflow.verifier import RuleBasedVerifier, ScriptedVerifier, Verifier

logger = logging.getLogger(__name__)


class WorkflowEngine:
    """Drive a :class:`Plan` through execute -> verify -> replan."""

    def __init__(
        self,
        *,
        executor: Any,
        verifier: Verifier | None = None,
        run_id: uuid.UUID | str | None = None,
        session_factory: Any = None,
        max_concurrency: int = 8,
    ) -> None:
        self._executor = executor
        self._verifier = verifier
        self._run_id = _opt_uuid(run_id)
        self._session_factory = session_factory
        self._max_concurrency = max(1, int(max_concurrency))

    # ------------------------------------------------------------------ #
    async def run(
        self,
        plan: Plan,
        *,
        verifier: Verifier | None = None,
        verifier_results: list | None = None,
        revise_step_ids: list[str] | None = None,
    ) -> WorkflowResult:
        """Execute ``plan`` to completion (or bounded failure).

        ``verifier_results`` is a convenience that wires a
        :class:`~app.agents.workflow.verifier.ScriptedVerifier` from bare
        verdict strings (``"pass"`` / ``"revise"`` / ``"fail"``) in one call;
        ``revise_step_ids`` names the steps a ``"revise"`` verdict reworks. The
        explicit ``verifier`` kwarg or the constructor verifier is used
        otherwise, falling back to :class:`RuleBasedVerifier`.
        """
        validate_plan(plan)
        verifier_impl = self._resolve_verifier(
            verifier=verifier,
            verifier_results=verifier_results,
            revise_step_ids=revise_step_ids,
        )

        observations: dict[str, StepObservation] = dict(plan.carry_observations)
        replans = 0
        peak_concurrency = 0
        verifier_history: list[VerifierResult] = []
        current = plan

        while True:
            step_observations, peak, failed_step = await self._execute_plan(
                current, observations
            )
            observations.update(step_observations)
            peak_concurrency = max(peak_concurrency, peak)

            # An execution failure short-circuits straight to ``failed``.
            if failed_step is not None:
                return WorkflowResult(
                    status="failed",
                    replans=replans,
                    max_concurrency=peak_concurrency,
                    observations=observations,
                    verifier_results=verifier_history,
                    error=f"step {failed_step!r} failed",
                )

            verdict = await verifier_impl.verify(current, observations)
            verifier_history.append(verdict)

            if verdict.verdict == VerificationVerdict.pass_:
                return WorkflowResult(
                    status="completed",
                    replans=replans,
                    max_concurrency=peak_concurrency,
                    observations=observations,
                    findings=verdict.findings,
                    verifier_results=verifier_history,
                )
            if verdict.verdict == VerificationVerdict.fail:
                return WorkflowResult(
                    status="failed",
                    replans=replans,
                    max_concurrency=peak_concurrency,
                    observations=observations,
                    findings=verdict.findings,
                    verifier_results=verifier_history,
                    error=verdict.note or "verification failed",
                )
            # revise
            if replans >= current.max_replans:
                return WorkflowResult(
                    status="failed",
                    replans=replans,
                    max_concurrency=peak_concurrency,
                    observations=observations,
                    findings=verdict.findings,
                    verifier_results=verifier_history,
                    error="replan budget exhausted",
                )
            # Consume one replan unit: produce a NEW versioned plan retaining
            # completed valid work and reworking only the flagged steps.
            current = revise_plan(
                current,
                revise_step_ids=verdict.revise_step_ids,
                observations=observations,
            )
            replans += 1
            await self._emit("plan.revised", {
                "version": current.version,
                "revise_step_ids": list(verdict.revise_step_ids),
                "replan_count": current.replan_count,
            })

    # ------------------------------------------------------------------ #
    def _resolve_verifier(
        self,
        *,
        verifier: Verifier | None,
        verifier_results: list | None,
        revise_step_ids: list[str] | None,
    ) -> Verifier:
        if verifier_results is not None:
            return ScriptedVerifier(
                verifier_results, revise_step_ids=list(revise_step_ids or [])
            )
        if verifier is not None:
            return verifier
        if self._verifier is not None:
            return self._verifier
        return RuleBasedVerifier()

    # ------------------------------------------------------------------ #
    async def _execute_plan(
        self,
        plan: Plan,
        seed_observations: dict[str, StepObservation],
    ) -> tuple[dict[str, StepObservation], int, str | None]:
        """Run every step respecting dependencies + bounded concurrency.

        Returns ``(observations, peak_concurrency, failed_step_id)``. When
        ``failed_step_id`` is not None the plan could not complete (a step
        failed permanently / exhausted its retries); the caller maps that to a
        terminal ``failed`` result.
        """
        observations: dict[str, StepObservation] = dict(seed_observations)
        # Carry-over (skip) observations must already be present (seeded from
        # plan.carry_observations by the caller).
        for s in plan.steps:
            if s.skip and s.id not in observations and s.id in plan.carry_observations:
                observations[s.id] = plan.carry_observations[s.id]

        events: dict[str, asyncio.Event] = {s.id: asyncio.Event() for s in plan.steps}
        state = _RunState()
        # Step ids that failed permanently. A downstream step whose dependency
        # failed is NOT ready — it would run with a missing upstream observation
        # and emit a misleading secondary error — so it is short-circuited.
        failed: set[str] = set()

        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def run_step(step: Step) -> tuple[str, StepObservation | None]:
            # Wait for all dependencies to finish (success OR failure).
            for dep in step.dependencies:
                await events[dep].wait()
            if any(dep in failed for dep in step.dependencies):
                # A dependency failed permanently; do not execute this step.
                # (The dep adds itself to ``failed`` before setting its event, so
                # by the time we pass ``wait()`` the membership check is sound.)
                failed.add(step.id)
                events[step.id].set()
                return step.id, None
            if step.skip:
                # Already-done retained work: nothing to execute.
                events[step.id].set()
                return step.id, observations.get(step.id)
            obs = await self._run_with_retries(step, observations, semaphore, state)
            if obs is None:
                # Step failed permanently; unblock waiters and surface failure.
                failed.add(step.id)
                events[step.id].set()
                return step.id, None
            observations[step.id] = obs
            events[step.id].set()
            return step.id, obs

        tasks = [asyncio.create_task(run_step(s)) for s in plan.steps]
        failed_step: str | None = None
        try:
            results = await asyncio.gather(*tasks)
            for sid, obs in results:
                if obs is None and sid is not None:
                    failed_step = sid
        except Exception:  # pragma: no cover - run_step traps its own errors
            # Defensive: cancel anything still in flight.
            for t in tasks:
                t.cancel()
            failed_step = next((s.id for s in plan.steps if not events[s.id].is_set()), None)

        return observations, state.peak, failed_step

    # ------------------------------------------------------------------ #
    async def _run_with_retries(
        self,
        step: Step,
        observations: dict[str, StepObservation],
        semaphore: asyncio.Semaphore,
        state: "_RunState",
    ) -> StepObservation | None:
        """Run ``step`` with retry + bounded concurrency + persistence.

        Returns the observation on success, or ``None`` when the step failed
        permanently / exhausted its retries.
        """
        policy = step.retry_policy
        attempt_number_base = await self._next_attempt_number(step.id)
        attempt = 0
        while True:
            attempt += 1
            attempt_number = attempt_number_base + attempt - 1
            try:
                await self._open_attempt(step.id, attempt_number)
                async with semaphore:
                    state.in_flight += 1
                    if state.in_flight > state.peak:
                        state.peak = state.in_flight
                    try:
                        obs = await self._executor.execute(step, dict(observations))
                    finally:
                        state.in_flight -= 1
                if obs.usage is None:
                    obs.usage = {"attempts": attempt}
                else:
                    obs.usage = {**dict(obs.usage), "attempts": attempt}
                obs.attempts = attempt
                await self._close_attempt(step.id, attempt_number, obs)
                return obs
            except BaseException as exc:  # noqa: BLE001 — executor may raise anything
                transient = policy.is_transient(exc) and attempt <= policy.max_retries
                await self._error_attempt(step.id, attempt_number, exc, transient)
                if transient:
                    continue
                logger.warning(
                    "workflow step %s failed permanently: %s", step.id, exc
                )
                return None

    # ------------------------------------------------------------------ #
    # Persistence helpers (best-effort, short-lived sessions)
    # ------------------------------------------------------------------ #
    async def _next_attempt_number(self, step_id: str) -> int:
        if not self._persistence_enabled():
            return 1
        try:
            async with self._session_factory() as sess:
                return await AttemptRepository(sess).next_attempt_number(
                    self._run_id, step_id
                )
        except Exception:  # pragma: no cover - best effort
            logger.debug("next_attempt_number failed for %s", step_id, exc_info=True)
            return 1

    async def _open_attempt(self, step_id: str, attempt_number: int) -> None:
        if not self._persistence_enabled():
            return
        try:
            async with self._session_factory() as sess:
                repo = AttemptRepository(sess)
                attempt = await repo.create_pending(
                    self._run_id, step_id, attempt_number=attempt_number
                )
                await repo.mark_running(attempt)
                await append_event_safe(
                    sess, self._run_id, "step.started",
                    {"step_id": step_id, "attempt": attempt_number},
                )
                await sess.commit()
        except Exception:  # pragma: no cover - best effort
            logger.debug("open_attempt failed for %s", step_id, exc_info=True)

    async def _close_attempt(
        self, step_id: str, attempt_number: int, obs: StepObservation
    ) -> None:
        if not self._persistence_enabled():
            return
        try:
            async with self._session_factory() as sess:
                from sqlalchemy import select

                from app.models import AgentAttempt

                repo = AttemptRepository(sess)
                # Re-fetch the running attempt for this step (highest number).
                result = await sess.execute(
                    select(AgentAttempt)
                    .where(
                        AgentAttempt.run_id == self._run_id,
                        AgentAttempt.step_key == step_id,
                    )
                    .order_by(AgentAttempt.attempt_number.desc())
                    .limit(1)
                )
                attempt = result.scalar_one_or_none()
                if attempt is not None and attempt.status == "running":
                    await repo.mark_done(attempt, usage=obs.usage)
                await append_event_safe(
                    sess, self._run_id, "step.completed",
                    {"step_id": step_id, "attempt": attempt_number},
                )
                await sess.commit()
        except Exception:  # pragma: no cover - best effort
            logger.debug("close_attempt failed for %s", step_id, exc_info=True)

    async def _error_attempt(
        self,
        step_id: str,
        attempt_number: int,
        exc: BaseException,
        transient: bool,
    ) -> None:
        if not self._persistence_enabled():
            return
        try:
            async with self._session_factory() as sess:
                from sqlalchemy import select

                from app.models import AgentAttempt

                result = await sess.execute(
                    select(AgentAttempt)
                    .where(
                        AgentAttempt.run_id == self._run_id,
                        AgentAttempt.step_key == step_id,
                    )
                    .order_by(AgentAttempt.attempt_number.desc())
                    .limit(1)
                )
                attempt = result.scalar_one_or_none()
                repo = AttemptRepository(sess)
                if attempt is not None and attempt.status == "running":
                    await repo.mark_error(attempt, str(exc))
                await append_event_safe(
                    sess, self._run_id, "step.failed",
                    {
                        "step_id": step_id,
                        "attempt": attempt_number,
                        "error": str(exc),
                        "transient": transient,
                    },
                )
                await sess.commit()
        except Exception:  # pragma: no cover - best effort
            logger.debug("error_attempt failed for %s", step_id, exc_info=True)

    async def _emit(self, event_type: str, data: dict) -> None:
        if not self._persistence_enabled():
            return
        try:
            async with self._session_factory() as sess:
                await append_event_safe(sess, self._run_id, event_type, data)
                await sess.commit()
        except Exception:  # pragma: no cover - best effort
            logger.debug("emit %s failed", event_type, exc_info=True)

    def _persistence_enabled(self) -> bool:
        return self._run_id is not None and self._session_factory is not None


# --------------------------------------------------------------------------- #
class _RunState:
    """Mutable holder for the in-flight counter + observed peak."""

    __slots__ = ("in_flight", "peak")

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0


def _opt_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None
