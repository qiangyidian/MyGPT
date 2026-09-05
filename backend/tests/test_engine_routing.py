"""Task 6b: route deep_research through the workflow engine behind a flag.

Three coverage points (strict TDD):

  1. Flag ON + deep_research profile -> the WorkflowEngine runs the turn
     (step_started/step_completed SSE events fire, the writer's output becomes
     the answer) AND the existing CrewAI runtime is NOT invoked.
  2. Flag OFF -> the engine path is skipped entirely; the existing CrewAI
     runtime runs unchanged.
  3. Flag ON but the engine raises -> the existing CrewAI path runs as the
     fallback (the user still gets an answer).

A deterministic fake executor (no LLM, no live endpoint) is injected via
``ctx.extra["workflow_executor"]`` so the engine path is exercisable without
crewai's real ``aexecute_task``. The existing CrewAI path is driven with the
already-proven ``FakeStageExecutor`` (injected via ``ctx.extra["stage_executor"]``).
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from sqlalchemy import select

from app.agents.intent_router import RouteDecision
from app.agents.orchestrator import ChatOrchestrator
from app.agents.runtime.crewai_runtime import CrewAIRuntime
from app.agents.schemas import AgentTurnContext, ExecutionMode
from app.agents.workflow.schemas import Step, StepObservation
from app.core.config import get_settings
from app.models import AgentAttempt, Conversation, Message
from tests.conftest import TestSessionLocal

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
async def _seed_ctx(db_session) -> AgentTurnContext:
    """Build a minimal deep_research turn context (expert-mode route)."""
    conv = Conversation(user_id=_SEEDED_USER, title="engine routing")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.flush()
    cfg = SimpleNamespace(
        provider="mock",
        api_base_url="http://x/v1",
        api_key_encrypted="",
        model_name="mock-model",
        temperature=0.3,
        top_p=1.0,
        max_tokens=64,
        supports_tools=True,
    )
    user = SimpleNamespace(id=_SEEDED_USER, role="user")
    ctx = AgentTurnContext(
        db=db_session,
        user=user,
        conversation=conv,
        model_config=cfg,
        request=SimpleNamespace(),
        user_content="compare A and B for a research report",
        system_prompt="",
        messages=[],
        rag_context="",
        citations=[],
        assistant_msg=msg,
        run_id=uuid.uuid4(),  # placeholder; the orchestrator overwrites this
        execution_mode=ExecutionMode.agent,
        agent_profile="deep_research",
        enable_tools=True,
    )
    ctx.extra["persistence_session_factory"] = TestSessionLocal
    ctx.extra["persistence_lock"] = asyncio.Lock()
    ctx.extra["db_mutation_lock"] = asyncio.Lock()
    # Expert-mode route: multi-agent deep_research.
    ctx.extra["route"] = RouteDecision(
        execution_mode=ExecutionMode.agent,
        agent_profile="deep_research",
        enable_tools=True,
        use_multi_agent=True,
        mode="expert",
        requested_mode="expert",
    )
    return ctx


class _FakeWorkflowExecutor:
    """Deterministic stand-in for the StageAdapterExecutor.

    Returns a canned observation per step (default ``[<id>] output``) so the
    engine completes without any LLM/endpoint. ``fail_step`` forces a raise at
    a specific step to exercise the fallback path.
    """

    def __init__(
        self,
        *,
        fail_step: str | None = None,
        outputs: dict[str, str] | None = None,
    ) -> None:
        self.fail_step = fail_step
        self.outputs = outputs or {}
        self.calls: list[str] = []

    async def execute(
        self, step: Step, upstream: dict[str, StepObservation]
    ) -> StepObservation:
        self.calls.append(step.id)
        if step.id == self.fail_step:
            raise RuntimeError(f"engine forced failure at {step.id}")
        out = self.outputs.get(step.id, f"[{step.id}] output")
        return StepObservation(step_id=step.id, output=out)


async def _drive(orchestrator: ChatOrchestrator, ctx) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    async for evt in orchestrator.stream(ctx):
        out.append((evt.kind, evt.data))
    return out


def _patch_flag(monkeypatch, *, engine: str = "", crewai: bool = True) -> None:
    """Patch the cached Settings instance (the orchestrator reads get_settings())."""
    s = get_settings()
    monkeypatch.setattr(s, "AGENT_WORKFLOW_ENGINE", engine, raising=False)
    monkeypatch.setattr(s, "CREWAI_ENABLED", crewai, raising=False)


def _spy_crewai(monkeypatch) -> dict:
    """Wrap CrewAIRuntime.stream_turn to count calls. Returns the counter dict."""
    calls = {"n": 0}
    original = CrewAIRuntime.stream_turn

    async def _spying(self, ctx):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        async for evt in original(self, ctx):
            yield evt

    monkeypatch.setattr(CrewAIRuntime, "stream_turn", _spying)
    return calls


# --------------------------------------------------------------------------- #
# 1. Flag ON + deep_research -> engine runs, CrewAI NOT invoked
# --------------------------------------------------------------------------- #
async def test_engine_runs_when_flag_on_and_profile_is_deep_research(
    db_session, monkeypatch
):
    _patch_flag(monkeypatch, engine="1", crewai=True)
    ctx = await _seed_ctx(db_session)
    ctx.extra["workflow_executor"] = _FakeWorkflowExecutor(
        outputs={
            "researcher": "gathered evidence",
            "analyst": "verified finding",
            "writer": "the final cited answer",
        }
    )
    crewai_calls = _spy_crewai(monkeypatch)

    events = await _drive(ChatOrchestrator(), ctx)
    kinds = [k for k, _ in events]

    # The engine ran the turn end-to-end.
    assert "agent_graph" in kinds, f"missing agent_graph in {kinds}"
    assert "step_started" in kinds, f"missing step_started in {kinds}"
    assert "step_completed" in kinds, f"missing step_completed in {kinds}"
    assert kinds[-1] == "done", f"expected done last, got {kinds[-1]}"
    # The writer's observation became the assistant answer.
    assert ctx.assistant_msg.content == "the final cited answer"
    # The fake executor observed the sequential Researcher->Analyst->Writer order.
    assert ctx.extra["workflow_executor"].calls == ["researcher", "analyst", "writer"]
    # The existing CrewAI path was NOT invoked.
    assert crewai_calls["n"] == 0, "CrewAI stream_turn must not run when the engine handles the turn"


# --------------------------------------------------------------------------- #
# 2. Flag OFF -> existing CrewAI path runs (unchanged)
# --------------------------------------------------------------------------- #
async def test_engine_skipped_when_flag_off_existing_crewai_runs(db_session, monkeypatch):
    _patch_flag(monkeypatch, engine="", crewai=True)
    ctx = await _seed_ctx(db_session)
    # Even if a workflow executor is injected, it MUST NOT be used when the flag is off.
    canary = _FakeWorkflowExecutor(fail_step="researcher")
    ctx.extra["workflow_executor"] = canary
    # The existing CrewAI path uses the proven FakeStageExecutor (no live LLM).
    from app.agents.runtime.stage_executor import FakeStageExecutor

    ctx.extra["stage_executor"] = FakeStageExecutor()
    crewai_calls = _spy_crewai(monkeypatch)

    events = await _drive(ChatOrchestrator(), ctx)
    kinds = [k for k, _ in events]

    # CrewAI ran.
    assert crewai_calls["n"] == 1, "the existing CrewAI path must run when the flag is off"
    # The engine executor was never consulted.
    assert canary.calls == [], "engine executor must not be invoked when the flag is off"
    # The turn completed normally via the CrewAI path.
    assert kinds[-1] == "done", f"expected done last, got {kinds[-1]}"


# --------------------------------------------------------------------------- #
# 3. Flag ON but engine raises -> CrewAI fallback runs
# --------------------------------------------------------------------------- #
async def test_engine_falls_back_to_crewai_on_exception(db_session, monkeypatch):
    _patch_flag(monkeypatch, engine="1", crewai=True)
    ctx = await _seed_ctx(db_session)
    # Engine executor raises on the first step.
    ctx.extra["workflow_executor"] = _FakeWorkflowExecutor(fail_step="researcher")
    # The CrewAI fallback needs a fake stage executor (no live LLM).
    from app.agents.runtime.stage_executor import FakeStageExecutor

    ctx.extra["stage_executor"] = FakeStageExecutor()
    crewai_calls = _spy_crewai(monkeypatch)

    events = await _drive(ChatOrchestrator(), ctx)
    kinds = [k for k, _ in events]

    # The engine attempted (and failed) — the canary executor recorded one call.
    assert ctx.extra["workflow_executor"].calls == ["researcher"]
    # ... then the CrewAI fallback ran.
    assert crewai_calls["n"] == 1, "CrewAI fallback must run when the engine raises"
    # The user still gets an answer (done, not error).
    assert kinds[-1] == "done", f"fallback must complete with done, got {kinds[-1]}"


# --------------------------------------------------------------------------- #
# 4. Engine run persists AgentAttempt rows (durable trace) when the flag is on
# --------------------------------------------------------------------------- #
async def test_engine_run_persists_agent_attempts(db_session, monkeypatch):
    _patch_flag(monkeypatch, engine="1", crewai=True)
    ctx = await _seed_ctx(db_session)
    ctx.extra["workflow_executor"] = _FakeWorkflowExecutor(
        outputs={"researcher": "ev", "analyst": "find", "writer": "answer"}
    )
    _spy_crewai(monkeypatch)

    await _drive(ChatOrchestrator(), ctx)

    # The engine was wired with run_id + session_factory, so each step persisted
    # at least one AgentAttempt row that reached 'done'.
    rows = (
        await db_session.execute(select(AgentAttempt))
    ).scalars().all()
    step_keys = {r.step_key for r in rows}
    assert {"researcher", "analyst", "writer"} <= step_keys, (
        f"expected attempts for all three steps, got {step_keys}"
    )
