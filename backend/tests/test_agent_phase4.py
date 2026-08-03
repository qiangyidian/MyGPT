"""Phase 4 acceptance tests: the Researcher/Analyst/Writer crew and the
reviewer quality gate. No live LLM is contacted — these verify structure,
handoff shape, the reviewer logic, and that plain chat never spawns a crew.
"""
from __future__ import annotations

import uuid

import pytest

from app.agents.crews import (
    AnalystFinding,
    ConflictNote,
    EvidenceItem,
    ResearchEvidence,
    build_research_crew,
    review_crew_output,
)
from app.agents.orchestrator import ChatOrchestrator
from app.agents.planning import classify_intent
from app.agents.runtime.crewai_runtime import CrewAIRuntime
from app.agents.runtime.native_runtime import NativeChatRuntime
from app.agents.schemas import ExecutionMode
from app.core.security import encrypt_secret
from app.models import ModelConfig
import types


# --------------------------------------------------------------------------- #
# Handoff schemas
# --------------------------------------------------------------------------- #
def test_evidence_and_finding_models_roundtrip():
    ev = ResearchEvidence(
        sub_questions=["q1"],
        evidence=[EvidenceItem(source="https://a", snippet="s", note="n")],
        gaps=["missing X"],
    )
    assert ev.evidence[0].source == "https://a"

    finding = AnalystFinding(
        sufficient=False,
        conflicts=[ConflictNote(claim="c", positions=["a (s1)", "b (s2)"])],
        verified_facts=["fact1"],
        unresolved=["X"],
        conclusion="insufficient",
    )
    assert finding.sufficient is False
    assert len(finding.conflicts) == 1


# --------------------------------------------------------------------------- #
# Crew construction
# --------------------------------------------------------------------------- #
def test_build_research_crew_has_three_roles_and_sequential_process():
    cfg = ModelConfig(
        name="t",
        provider="openai-compatible",
        api_base_url="http://localhost:8000/v1",
        api_key_encrypted=encrypt_secret("sk-test"),
        model_name="gpt-4o",
        temperature=0.3,
        max_tokens=64,
    )
    from app.agents.adapters.llm_adapter import CrewAILLMFactory

    llm = CrewAILLMFactory.from_model_config(cfg)
    crew = build_research_crew(
        llm=llm, tools=[], question="compare X vs Y", run_id=uuid.uuid4()
    )
    roles = [a.role for a in crew.agents]
    assert roles == ["Researcher", "Analyst", "Writer"]
    # Writer has no tools (cannot fetch new facts).
    assert crew.agents[2].tools is None or crew.agents[2].tools == []
    # Three tasks, chained: analyze context includes research; write includes both.
    assert len(crew.tasks) == 3
    assert crew.tasks[0] in (crew.tasks[1].context or [])
    assert crew.tasks[0] in (crew.tasks[2].context or [])
    assert crew.tasks[1] in (crew.tasks[2].context or [])


# --------------------------------------------------------------------------- #
# Reviewer gate
# --------------------------------------------------------------------------- #
def test_reviewer_passes_cited_answer_with_no_gaps():
    ev = ResearchEvidence(sub_questions=["q"], evidence=[EvidenceItem(source="s1", snippet="x")], gaps=[])
    r = review_crew_output("See [source 1] for details.", ev)
    assert r["passed"] is True
    assert r["has_citation"] is True


def test_reviewer_fails_uncited_answer():
    ev = ResearchEvidence(sub_questions=["q"], evidence=[EvidenceItem(source="s1", snippet="x")], gaps=[])
    r = review_crew_output("The answer is 42.", ev)
    assert r["passed"] is False
    assert r["has_citation"] is False


def test_reviewer_requires_limitation_acknowledgement_when_gaps_exist():
    ev = ResearchEvidence(
        sub_questions=["q"],
        evidence=[EvidenceItem(source="s1", snippet="x")],
        gaps=["could not find Y"],
    )
    # Cited but ignores the gap -> fail.
    assert review_crew_output("Result is X [source 1].", ev)["passed"] is False
    # Cited and acknowledges insufficiency -> pass.
    assert review_crew_output("Partial answer: X [source 1]; evidence insufficient for Y.", ev)["passed"] is True


# --------------------------------------------------------------------------- #
# Plain chat must not spawn a crew
# --------------------------------------------------------------------------- #
def test_plain_chat_never_uses_crewai(monkeypatch):
    settings = __import__("app.core.config", fromlist=["get_settings"]).get_settings()
    monkeypatch.setattr(settings, "CREWAI_ENABLED", True)

    orch = ChatOrchestrator()
    # chat and auto always use native, even with crewai available.
    for mode in (ExecutionMode.chat, ExecutionMode.auto):
        ctx = types.SimpleNamespace(
            execution_mode=mode,
            extra={"route": types.SimpleNamespace(
                use_multi_agent=False, requested_mode="auto", mode="auto", agent_profile="general",
            )},
        )
        runtime, _ = orch._select_runtime(ctx)
        assert isinstance(runtime, NativeChatRuntime)

    # agent mode (multi-agent) selects CrewAI.
    ctx = types.SimpleNamespace(
        execution_mode=ExecutionMode.agent,
        extra={"route": types.SimpleNamespace(
            use_multi_agent=True, requested_mode="debate", mode="debate", agent_profile="debate",
        )},
    )
    runtime, sel = orch._select_runtime(ctx)
    assert isinstance(runtime, CrewAIRuntime)
    assert sel.multi_agent_executed is True


def test_research_intent_routes_to_crew_in_runtime():
    """The runtime's intent classification sends deep_research to the crew
    path (verified by plan step count)."""
    rt = CrewAIRuntime()
    # We can't run kickoff without a live LLM, but we can confirm the routing
    # decision: classify_intent on a research query yields deep_research, whose
    # plan has 3 steps (the three roles).
    intent = classify_intent("帮我深度研究对比两个框架")
    assert intent == "deep_research"
    from app.agents.planning import build_plan

    _, steps = build_plan(intent, "compare frameworks")
    assert len(steps) == 3
