"""Demo-data leakage regression tests.

Guards the bug where a normal chat turn ("你都能干什么") was answered with the
hard-coded CrewAI demo text. Covers: AGENT_DEMO_MODE default + prod guard, auto
routing of casual/capability questions to native, demo-executor isolation behind
an explicit per-request opt-in, citation-marker integrity, and RAG pollution
skipping for casual questions while preserving explicit KB retrieval.
"""
from __future__ import annotations

import types
import uuid

import pytest

from app.agents.intent_router import decide_route
from app.agents.runtime.crewai_runtime import CrewAIRuntime
from app.agents.runtime.native_runtime import NativeChatRuntime
from app.agents.runtime.stage_executor import DemoStageExecutor
from app.agents.schemas import AgentEvent, ExecutionMode
from app.agents.orchestrator import ChatOrchestrator
from app.agents.planning import is_casual_question
from app.rag.citations import sanitize_unbacked_source_markers

# The exact canned string that previously leaked into normal chat.
CANNED_DEMO_ANSWER = (
    "CrewAI supports stateful Flows [source 1] and parallel agent "
    "execution via gather [source 2]. The dual-runtime design keeps "
    "native chat unaffected [source 2]."
)

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


# =========================================================================== #
# Config: AGENT_DEMO_MODE default + production guard
# =========================================================================== #
def test_default_agent_demo_mode_is_false():
    """The declared default must be False — never rely on operators remembering
    to turn demo off in production."""
    from app.core.config import Settings

    assert Settings.model_fields["AGENT_DEMO_MODE"].default is False


def test_prod_rejects_agent_demo_mode():
    """ENV=prod + AGENT_DEMO_MODE=true must refuse to boot (not warn-and-go)."""
    from app.core.config import Settings

    with pytest.raises(ValueError) as exc:
        Settings(
            ENV="prod",
            AGENT_DEMO_MODE=True,
            JWT_SECRET="a-strong-random-secret",
            ADMIN_PASSWORD="a-changed-password",
        )
    assert "AGENT_DEMO_MODE" in str(exc.value)


def test_prod_allows_demo_off():
    """ENV=prod with AGENT_DEMO_MODE=False boots fine (negative control)."""
    from app.core.config import Settings

    s = Settings(
        ENV="prod",
        AGENT_DEMO_MODE=False,
        JWT_SECRET="a-strong-random-secret",
        ADMIN_PASSWORD="a-changed-password",
    )
    assert s.AGENT_DEMO_MODE is False


# =========================================================================== #
# Routing: auto + casual/capability questions stay native
# =========================================================================== #
def test_auto_capability_question_routes_native():
    """'你都能干什么' under auto must NOT escalate to the multi-agent/demo path."""
    r = decide_route("auto", user_content="你都能干什么")
    assert r.execution_mode == ExecutionMode.auto
    assert r.use_multi_agent is False
    assert r.mode == "auto"


def test_auto_greeting_routes_native():
    for greeting in ("你好", "您好", "在吗", "hello"):
        r = decide_route("auto", user_content=greeting)
        assert r.use_multi_agent is False, f"{greeting!r} must stay native"
        assert r.execution_mode == ExecutionMode.auto


def test_explicit_deep_research_routes_multi_agent():
    """An explicit deep_research selection still routes to the multi-agent
    runtime (the demo gating, not the router, prevents canned answers)."""
    r = decide_route("deep_research", has_knowledge_base=False)
    assert r.execution_mode == ExecutionMode.agent
    assert r.use_multi_agent is True
    assert r.agent_profile == "deep_research"


# =========================================================================== #
# Orchestrator: demo isolation — never on a normal turn, only on explicit opt-in
# =========================================================================== #
def _ctx(*, multi_agent: bool, mode: str = "auto", profile: str = "general", demo: bool = False):
    return types.SimpleNamespace(
        execution_mode=ExecutionMode.agent if multi_agent else ExecutionMode.auto,
        agent_profile=profile,
        request=types.SimpleNamespace(demo=demo),
        extra={"route": types.SimpleNamespace(
            use_multi_agent=multi_agent, requested_mode=mode, mode=mode, agent_profile=profile,
        )},
    )


def _settings(monkeypatch, *, crewai: bool, demo_env: bool):
    settings = __import__("app.core.config", fromlist=["get_settings"]).get_settings()
    monkeypatch.setattr(settings, "CREWAI_ENABLED", crewai)
    monkeypatch.setattr(settings, "AGENT_DEMO_MODE", demo_env)
    return settings


def test_demo_executor_not_used_for_normal_chat(monkeypatch):
    """With AGENT_DEMO_MODE on but NO per-request demo flag, a multi-agent
    request falls back to native — never the demo executor / canned content."""
    _settings(monkeypatch, crewai=False, demo_env=True)
    orch = ChatOrchestrator()

    # deep_research request, demo flag absent → native fallback, is_demo False.
    runtime, sel = orch._select_runtime(_ctx(multi_agent=True, mode="deep_research"))
    assert isinstance(runtime, NativeChatRuntime)
    assert sel.is_demo is False
    assert sel.multi_agent_executed is False
    assert sel.fallback_reason == "crewai_disabled"

    # A non-multi-agent (auto) turn → native, is_demo False.
    runtime2, sel2 = orch._select_runtime(_ctx(multi_agent=False))
    assert isinstance(runtime2, NativeChatRuntime)
    assert sel2.is_demo is False


def test_demo_executor_only_on_explicit_request_flag(monkeypatch):
    """Demo content is reachable ONLY with the explicit per-request demo flag."""
    _settings(monkeypatch, crewai=False, demo_env=True)
    orch = ChatOrchestrator()

    runtime, sel = orch._select_runtime(
        _ctx(multi_agent=True, mode="deep_research", demo=True)
    )
    assert isinstance(runtime, CrewAIRuntime)
    assert sel.is_demo is True
    assert sel.multi_agent_executed is True
    assert sel.fallback_reason is None


def test_demo_executor_disabled_when_real_crewai_enabled():
    """When real CrewAI is enabled, the demo executor NEVER runs — even with the
    demo flag set. This keeps the runtime's demo decision in lock-step with the
    orchestrator's is_demo (which drives the UI warning), so canned content is
    never served without its banner."""
    from app.agents.runtime.crewai_runtime import _demo_executor_enabled

    def settings(*, crewai, demo_env):
        return types.SimpleNamespace(CREWAI_ENABLED=crewai, AGENT_DEMO_MODE=demo_env)

    req_demo = types.SimpleNamespace(demo=True)
    req_plain = types.SimpleNamespace(demo=False)

    # Real CrewAI wins → demo executor never used, even with the demo flag.
    assert _demo_executor_enabled(settings(crewai=True, demo_env=True), req_demo) is False
    # No real CrewAI + explicit demo opt-in → demo executor used.
    assert _demo_executor_enabled(settings(crewai=False, demo_env=True), req_demo) is True
    # No per-request demo flag → never (the public chat path).
    assert _demo_executor_enabled(settings(crewai=False, demo_env=True), req_plain) is False
    assert _demo_executor_enabled(settings(crewai=True, demo_env=True), req_plain) is False
    # Demo env off → never.
    assert _demo_executor_enabled(settings(crewai=False, demo_env=False), req_demo) is False


def test_no_canned_crewai_answer_for_capability_question(monkeypatch):
    """The canned demo answer must not appear in any NORMAL runtime path. It
    lives only in the demo-only module consumed by DemoStageExecutor, which is
    itself gated behind the explicit demo opt-in."""
    _settings(monkeypatch, crewai=False, demo_env=True)
    orch = ChatOrchestrator()

    # Normal capability question under auto → native, not demo.
    runtime, sel = orch._select_runtime(_ctx(multi_agent=False))
    assert isinstance(runtime, NativeChatRuntime)
    assert sel.is_demo is False

    # The canned text exists ONLY inside the demo behaviours fixture.
    from app.agents.runtime.demo_content import build_demo_behaviours

    behaviours = build_demo_behaviours()
    assert CANNED_DEMO_ANSWER == behaviours["writer"].output
    # And it is not referenced by the real executors.
    assert not hasattr(NativeChatRuntime, "_defaults")


def test_crewai_unavailable_never_falls_back_to_demo_content(monkeypatch):
    """deep_research + CrewAI unavailable must degrade to native with a visible
    reason — it must NEVER substitute the canned demo answer."""
    _settings(monkeypatch, crewai=False, demo_env=True)  # demo env on ...
    orch = ChatOrchestrator()
    # ... but the request does NOT opt into demo:
    runtime, sel = orch._select_runtime(_ctx(multi_agent=True, mode="deep_research", demo=False))
    assert isinstance(runtime, NativeChatRuntime)
    assert sel.is_demo is False
    assert sel.multi_agent_executed is False
    assert sel.fallback_reason == "crewai_disabled"


# =========================================================================== #
# Citation integrity
# =========================================================================== #
def test_unbacked_source_markers_are_rejected():
    # No citations → every marker stripped.
    text, changed = sanitize_unbacked_source_markers("blah [source 1]", 0)
    assert text == "blah"
    assert changed is True

    # One citation → [source 2] has no backing, stripped; [source 1] kept.
    text, changed = sanitize_unbacked_source_markers("a [source 1] b [source 2]", 1)
    assert "[source 1]" in text
    assert "[source 2]" not in text
    assert changed is True

    # All markers backed → unchanged.
    text, changed = sanitize_unbacked_source_markers("x [source 1] y [source 2]", 2)
    assert text == "x [source 1] y [source 2]"
    assert changed is False

    # No markers → unchanged.
    text, changed = sanitize_unbacked_source_markers("plain answer", 0)
    assert text == "plain answer"
    assert changed is False

    # Chinese 来源 marker + case-insensitive 'Source'.
    text, changed = sanitize_unbacked_source_markers("数据见[来源 3]", 0)
    assert "[来源 3]" not in text
    assert changed is True
    text, _ = sanitize_unbacked_source_markers("see [Source 1]", 1)
    assert text == "see [Source 1]"

    # No-space and colon variants a model might emit are also stripped/kept.
    text, changed = sanitize_unbacked_source_markers("see [source1] and [source: 2]", 1)
    assert "[source1]" in text          # backed (n=1) — kept
    assert "[source: 2]" not in text    # unbacked (n=2) — stripped
    assert changed is True
    text, _ = sanitize_unbacked_source_markers("fullwidth [来源：1]", 1)
    assert "[来源：1]" in text           # backed — kept


# =========================================================================== #
# Casual-question detection (RAG pollution guard)
# =========================================================================== #
@pytest.mark.parametrize("text", [
    "你好", "你是谁", "你都能干什么", "介绍一下自己", "谢谢", "帮助",
    "你能做什么呢？", "请介绍一下你自己", "ok", "嗯嗯",
])
def test_casual_detector_flags_social_and_capability(text):
    assert is_casual_question(text) is True


@pytest.mark.parametrize("text", [
    "请根据知识库总结产品规格",  # explicit KB ask
    "对比一下 React 和 Vue 的优缺点",  # real research
    "帮我写一个贪吃蛇",  # code request
    "你好，请帮我查询订单状态",  # greeting + real request
])
def test_casual_detector_does_not_flag_real_questions(text):
    assert is_casual_question(text) is False


# =========================================================================== #
# RAG: casual skips conversation-bound KB; explicit/real question still retrieves
# =========================================================================== #
async def _drain(stream):
    out = []
    async for evt in stream:
        out.append(evt)
    return out


async def _make_kb_conversation(db_session, *, content: str, kb_ids, mode: str = "auto"):
    from app.models import Conversation, KnowledgeBase, ModelConfig
    from app.schemas import ChatRequest

    cfg = ModelConfig(
        user_id=None, name="mock", provider="mock",
        api_base_url="http://localhost/v1", model_name="mock-model",
        supports_stream=True, is_embedding=False,
    )
    db_session.add(cfg)
    await db_session.flush()

    kb = KnowledgeBase(user_id=_SEEDED_USER, name="KB")
    db_session.add(kb)
    await db_session.flush()

    conv = Conversation(user_id=_SEEDED_USER, title="c", knowledge_base_id=kb.id)
    db_session.add(conv)
    await db_session.commit()

    request = ChatRequest(
        conversation_id=conv.id,
        content=content,
        mode=mode,
        knowledge_base_ids=list(kb_ids) if kb_ids else [],
    )
    from app.models import User
    user = (await db_session.execute(
        __import__("sqlalchemy").select(User).where(User.id == _SEEDED_USER)
    )).scalar_one()
    return request, user


def _stub_runtime(monkeypatch, retrieve_calls):
    """Replace the LLM runtime with a no-op done event, and record RAG calls."""
    from app.services.chat_service import chat_service
    from app.agents.orchestrator import chat_orchestrator
    from app.rag.rag_service import rag_service

    async def fake_retrieve(db, question, kb_ids, top_k=None):
        retrieve_calls.append((question, list(kb_ids)))
        return "CTX", []

    async def fake_stream(ctx):
        ctx.assistant_msg.content = "ok"
        ctx.extra["runtime_selection"] = None
        yield AgentEvent(kind="done", data={
            "message_id": str(ctx.assistant_msg.id), "finish_reason": "stop",
        })

    monkeypatch.setattr(rag_service, "retrieve", fake_retrieve)
    monkeypatch.setattr(chat_orchestrator, "stream", fake_stream)
    return chat_service


async def test_casual_question_skips_conversation_kb_rag(db_session, monkeypatch):
    """A greeting against a conversation whose KB is only INHERITED must not
    trigger retrieval (the pollution source)."""
    from app.models import KnowledgeBase
    request, user = await _make_kb_conversation(db_session, content="你好", kb_ids=[])
    # Resolve the conversation's inherited KB id to assert it exists.
    from sqlalchemy import select
    from app.models import Conversation
    conv = (await db_session.execute(
        select(Conversation).where(Conversation.id == request.conversation_id)
    )).scalar_one()
    assert conv.knowledge_base_id is not None  # a KB IS bound...

    retrieve_calls: list = []
    chat_service = _stub_runtime(monkeypatch, retrieve_calls)
    await _drain(chat_service.stream(db=db_session, user=user, request=request))
    assert retrieve_calls == []  # ...yet retrieval was skipped (casual)


async def test_explicit_kb_question_still_uses_rag(db_session, monkeypatch):
    """A genuine question against an explicit KB selection still retrieves."""
    from app.models import KnowledgeBase
    from sqlalchemy import select
    kb = KnowledgeBase(user_id=_SEEDED_USER, name="KB2")
    db_session.add(kb)
    await db_session.flush()

    from app.models import Conversation
    conv = Conversation(user_id=_SEEDED_USER, title="c2")
    db_session.add(conv)
    await db_session.commit()

    from app.models import ModelConfig, User
    from app.schemas import ChatRequest
    cfg = ModelConfig(
        user_id=None, name="mock", provider="mock",
        api_base_url="http://localhost/v1", model_name="mock-model",
        supports_stream=True, is_embedding=False,
    )
    db_session.add(cfg)
    await db_session.commit()
    user = (await db_session.execute(select(User).where(User.id == _SEEDED_USER))).scalar_one()

    request = ChatRequest(
        conversation_id=conv.id,
        content="请根据知识库总结产品规格的主要参数",
        mode="auto",
        knowledge_base_ids=[kb.id],
    )

    retrieve_calls: list = []
    chat_service = _stub_runtime(monkeypatch, retrieve_calls)
    await _drain(chat_service.stream(db=db_session, user=user, request=request))
    assert len(retrieve_calls) == 1
    assert retrieve_calls[0][1] == [kb.id]
