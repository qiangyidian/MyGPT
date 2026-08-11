"""Durable workflow layer.

Task 4 / 5 — transactional repositories for commands and leases + the durable
worker:

  * :class:`~app.agents.workflow.repository.CommandStore` — the exactly-once
    control-command queue (pause/resume/cancel/instruction/approve/reject).
  * :class:`~app.agents.workflow.repository.LeaseStore` — run execution leases
    with optimistic fencing (consumed by Task 5).
  * :mod:`~app.agents.workflow.controls` — high-level persist-first writers the
    API layer calls before publishing the live wake-up signal.

Task 6 — planner-executor-verifier state machine (generalizes the static graph
into a verifiable plan->execute->verify->replan engine):

  * :mod:`~app.agents.workflow.schemas` — :class:`Plan`, :class:`Step`,
    :class:`VerifierResult`, :class:`VerificationVerdict`, retry/error types.
  * :mod:`~app.agents.workflow.planner` — plan construction + validation
    (topological sort) + template builders per profile + bounded revision.
  * :mod:`~app.agents.workflow.executor` — :class:`StepExecutor` protocol +
    default/recording stubs + a CrewAI :class:`StageAdapterExecutor`.
  * :mod:`~app.agents.workflow.verifier` — :class:`Verifier` protocol + a
    rule-based verifier + a scripted test double.
  * :mod:`~app.agents.workflow.engine` — :class:`WorkflowEngine.run`:
    execute->verify->(bounded) replan with peak-concurrency observation.
  * :mod:`~app.agents.workflow.attempts` — :class:`AttemptRepository` over the
    ``agent_attempts`` table (per-attempt usage / retry accounting).

The in-memory ``run_controls`` + ``approval_bus`` remain the live-runtime
signal; this package is the durable source of truth beneath them.
"""
from __future__ import annotations
