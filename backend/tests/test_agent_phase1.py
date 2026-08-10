"""Phase 1 acceptance tests: the CrewAI runtime is wired, selectable, and its
adapters bridge onto the existing ModelConfig / ToolGateway contracts.

These do NOT require a live LLM (CrewAI uses LiteLLM, not our MockProvider).
They verify: orchestrator selection, the LLM factory, the tool-adapter shape,
the adapter's ``_run`` bridging through the gateway, and event mapping.
"""
from __future__ import annotations

import types
import uuid

import pytest

from app.agents.adapters.event_adapter import map_crewai_event
from app.agents.adapters.llm_adapter import CrewAILLMFactory
from app.agents.adapters.tool_adapter import build_crewai_tool
from app.agents.orchestrator import ChatOrchestrator
from app.agents.runtime.crewai_runtime import CrewAIRuntime
from app.agents.runtime.native_runtime import NativeChatRuntime
from app.agents.schemas import ExecutionMode, ev_token, ev_tool_call
from app.core.security import encrypt_secret
from app.models import ModelConfig, ToolCall
from app.tools.builtin import DateTimeNowTool

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


# --------------------------------------------------------------------------- #
# Orchestrator selection
# --------------------------------------------------------------------------- #
def _ctx(execution_mode, *, multi_agent: bool, mode: str = "auto", profile: str = "general"):
    return types.SimpleNamespace(
        execution_mode=execution_mode,
        extra={"route": types.SimpleNamespace(
            use_multi_agent=multi_agent, requested_mode=mode, mode=mode, agent_profile=profile,
        )},
    )


def test_orchestrator_native_when_crewai_disabled(monkeypatch):
    settings = __import__("app.core.config", fromlist=["get_settings"]).get_settings()
    monkeypatch.setattr(settings, "CREWAI_ENABLED", False)
    monkeypatch.setattr(settings, "AGENT_DEMO_MODE", False)

    orch = ChatOrchestrator()
    available, reason = orch._crewai_status()
    assert available is False
    assert reason == "crewai_disabled"
    # A multi-agent request when crewai is off → explicit native FALLBACK
    # (observable, not silent) with multi_agent_executed=False.
    ctx = _ctx(ExecutionMode.agent, multi_agent=True, mode="debate", profile="debate")
    runtime, sel = orch._select_runtime(ctx)
    assert isinstance(runtime, NativeChatRuntime)
    assert sel.multi_agent_requested is True
    assert sel.multi_agent_executed is False
    assert sel.fallback_reason == "crewai_disabled"
    # A plain (non-multi-agent) turn → native, no fallback.
    runtime2, sel2 = orch._select_runtime(_ctx(ExecutionMode.auto, multi_agent=False))
    assert isinstance(runtime2, NativeChatRuntime)
    assert sel2.multi_agent_requested is False
    assert sel2.fallback_reason is None


def test_orchestrator_crewai_when_enabled_and_agent_mode(monkeypatch):
    settings = __import__("app.core.config", fromlist=["get_settings"]).get_settings()
    monkeypatch.setattr(settings, "CREWAI_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_DEMO_MODE", False)

    orch = ChatOrchestrator()
    available, reason = orch._crewai_status()
    assert available is True
    assert reason is None
    ctx = _ctx(ExecutionMode.agent, multi_agent=True, mode="debate", profile="debate")
    runtime, sel = orch._select_runtime(ctx)
    assert isinstance(runtime, CrewAIRuntime)
    assert sel.multi_agent_executed is True
    assert sel.fallback_reason is None

    # chat/auto still use native even when crewai is available.
    runtime2, _ = orch._select_runtime(_ctx(ExecutionMode.chat, multi_agent=False))
    assert isinstance(runtime2, NativeChatRuntime)


# --------------------------------------------------------------------------- #
# LLM factory
# --------------------------------------------------------------------------- #
def test_crewai_llm_factory_from_model_config():
    cfg = ModelConfig(
        name="t",
        provider="openai-compatible",
        api_base_url="http://localhost:8000/v1",
        api_key_encrypted=encrypt_secret("sk-test"),
        model_name="gpt-4o",
        temperature=0.5,
        top_p=0.9,
        max_tokens=128,
    )
    llm = CrewAILLMFactory.from_model_config(cfg)
    # CrewAI parses the "openai/" prefix into provider + bare model.
    assert llm.model == "gpt-4o"
    assert llm.provider == "openai"  # openai-compatible routing
    assert llm.base_url == "http://localhost:8000/v1"
    assert llm.api_key == "sk-test"  # decrypted, not the ciphertext


@pytest.mark.parametrize(
    ("parameter", "expected_key"),
    [
        ("max_tokens", "max_tokens"),
        ("max_completion_tokens", "max_completion_tokens"),
    ],
)
def test_crewai_llm_factory_uses_exact_output_parameter(
    monkeypatch, parameter: str, expected_key: str
):
    captured = {}

    class FakeLLM(types.SimpleNamespace):
        def call(self, *args, **kwargs):
            return "ok"

        async def acall(self, *args, **kwargs):
            return "ok"

    def fake_llm(**kwargs):
        captured.update(kwargs)
        return FakeLLM(**kwargs)

    import crewai

    monkeypatch.setattr(crewai, "LLM", fake_llm)
    cfg = ModelConfig(
        name="reasoner",
        provider="openai-compatible",
        api_base_url="http://localhost:8000/v1",
        api_key_encrypted=encrypt_secret("sk-test"),
        model_name="reasoner",
        temperature=0.3,
        max_tokens=321,
        output_token_parameter=parameter,
    )

    CrewAILLMFactory.from_model_config(cfg)

    assert captured[expected_key] == 321
    assert captured["max_retries"] == 0
    other_key = (
        "max_completion_tokens" if expected_key == "max_tokens" else "max_tokens"
    )
    assert other_key not in captured


# --------------------------------------------------------------------------- #
# Tool adapter
# --------------------------------------------------------------------------- #
def test_build_crewai_tool_shape():
    adapter = build_crewai_tool(
        DateTimeNowTool(),
        conversation_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        user_id=_SEEDED_USER,
    )
    assert adapter.name == "datetime_now"
    assert adapter.description  # non-empty
    assert adapter.args_schema is not None


async def test_crewai_tool_adapter_run_bridges_to_gateway(db_session, monkeypatch):
    """The adapter's sync ``_run`` opens a fresh session and runs the tool
    through the gateway, persisting a ToolCall row."""
    from app.models import AgentRun, Conversation, Message

    conv = Conversation(user_id=_SEEDED_USER, title="crewai adapter")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id,
        message_id=msg.id,
        user_id=_SEEDED_USER,
        runtime="crewai",
        flow_name="test",
        status="running",
    )
    db_session.add(run)
    await db_session.flush()

    # Point the adapter's fresh-session factory at the in-memory test DB.
    from tests.conftest import TestSessionLocal
    import app.db as db_mod

    monkeypatch.setattr(db_mod, "AsyncSessionLocal", TestSessionLocal)

    adapter = build_crewai_tool(
        DateTimeNowTool(),
        conversation_id=conv.id,
        message_id=msg.id,
        run_id=run.id,
        user_id=_SEEDED_USER,
    )
    out = adapter._run()  # sync; bridges async gateway internally
    assert isinstance(out, str)
    assert "utc_iso" in out  # datetime_now payload stringified

    # The gateway persisted a ToolCall row.
    rows = (
        await db_session.execute(
            __import__("sqlalchemy").select(ToolCall).where(ToolCall.message_id == msg.id)
        )
    ).scalars().all()
    assert len(rows) == 1 and rows[0].tool_name == "datetime_now"


# --------------------------------------------------------------------------- #
# Event adapter (defensive mapping)
# --------------------------------------------------------------------------- #
def test_event_adapter_maps_token_and_tool():
    tok = map_crewai_event({"type": "llm_stream_chunk", "chunk": "hi"})
    assert tok is not None and tok.kind == "token" and tok.data["delta"] == "hi"

    tool = map_crewai_event(
        {"type": "tool_usage_started", "tool_name": "web_search", "arguments": {"query": "x"}}
    )
    assert tool is not None and tool.kind == "tool_call"
    assert tool.data["name"] == "web_search"

    ignored = map_crewai_event({"type": "something_unknown"})
    assert ignored is None
