"""Native runtime graph-event tests (scope C).

The native single-agent path now emits a single-node ``agent_graph`` +
``agent_status`` + ``run_status`` + a step lifecycle, and attributes
``tool_call``/``tool_result`` to the ``assistant`` node — so even plain
(non-crew) turns surface in the agent panel.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.agents.graph import build_single_agent_graph
from app.agents.runtime.native_runtime import NativeChatRuntime
from app.agents.schemas import AgentTurnContext, ExecutionMode
from app.models import AgentRun, Conversation, Message
from app.providers.base import ChatDelta, ToolCallDef

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


# --------------------------------------------------------------------------- #
# Pure unit: single-agent graph builder
# --------------------------------------------------------------------------- #
def test_single_agent_graph_has_one_assistant_node():
    g = build_single_agent_graph("anything").to_public_dict()
    assert g["runtime"] == "native"
    assert g["mode"] == "sequential"
    assert [n["id"] for n in g["nodes"]] == ["assistant"]
    assert g["edges"] == []


# --------------------------------------------------------------------------- #
# Fakes: deterministic, instant provider + gateway (no network)
# --------------------------------------------------------------------------- #
class _FakeProvider:
    """Yields a scripted sequence of deltas, one list per stream_chat call."""

    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.calls = []

    async def stream_chat(self, messages, options=None):
        self.calls.append(list(messages))
        for delta in self._rounds.pop(0):
            yield delta


class _FakeExecution(SimpleNamespace):
    full_result = None

    def to_openai_tool_message(self):
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "content": getattr(self, "content", '{"content": "search hits"}'),
        }


class _FakeGateway:
    def __init__(self, *a, **kw):
        pass

    async def execute(self, *, tool_call_id, tool_name, arguments):
        return _FakeExecution(
            status="success", ok=True, approval_id=None,
            result={"content": "search hits"}, error=None,
            tool_call_id=tool_call_id, name=tool_name,
        )


async def _seed_native_ctx(db_session, *, enable_tools: bool) -> AgentTurnContext:
    conv = Conversation(user_id=_SEEDED_USER, title="native graph test")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id, message_id=msg.id, user_id=_SEEDED_USER,
        runtime="native", flow_name="single_agent", status="running",
    )
    db_session.add(run)
    await db_session.flush()
    cfg = SimpleNamespace(
        provider="mock", api_base_url="http://x/v1", api_key_encrypted="",
        model_name="mock", temperature=0.3, top_p=1.0, max_tokens=64,
        supports_tools=True,
    )
    user = SimpleNamespace(id=_SEEDED_USER, role="user")
    ctx = AgentTurnContext(
        db=db_session, user=user, conversation=conv, model_config=cfg,
        request=SimpleNamespace(), user_content="hello world",
        system_prompt="", messages=[], rag_context="", citations=[],
        assistant_msg=msg, run_id=run.id, execution_mode=ExecutionMode.auto,
        agent_profile="general", enable_tools=enable_tools,
    )
    return ctx


async def _collect(ctx) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    async for evt in NativeChatRuntime().stream_turn(ctx):
        out.append((evt.kind, evt.data))
    return out


def _find_all(events, kind):
    return [d for k, d in events if k == kind]


# --------------------------------------------------------------------------- #
# Runtime: core lifecycle (no tools)
# --------------------------------------------------------------------------- #
async def test_native_emits_single_node_graph_and_lifecycle(db_session, monkeypatch):
    ctx = await _seed_native_ctx(db_session, enable_tools=False)
    monkeypatch.setattr(
        "app.agents.runtime.native_runtime.get_provider_for_config",
        lambda cfg: _FakeProvider([
            [ChatDelta(content="Hello"), ChatDelta(finish_reason="stop")],
        ]),
    )

    events = await _collect(ctx)
    kinds = [k for k, _ in events]

    graphs = _find_all(events, "agent_graph")
    assert graphs, "expected an agent_graph event"
    nodes = graphs[0]["graph"]["nodes"]
    assert [n["id"] for n in nodes] == ["assistant"]

    statuses = [d["status"] for d in _find_all(events, "agent_status")
                if d["agent_id"] == "assistant"]
    assert statuses[0] == "running"
    assert statuses[-1] == "completed"

    runs = _find_all(events, "run_status")
    assert runs[0]["status"] == "running"
    assert runs[0]["current_agent_ids"] == ["assistant"]
    assert runs[-1]["status"] == "completed"
    assert runs[-1]["current_agent_ids"] == []

    starts = {d["step_id"] for d in _find_all(events, "step_started")}
    completes = {d["step_id"] for d in _find_all(events, "step_completed")}
    assert "answer" in starts and "answer" in completes
    assert "plan" not in starts  # no plan step without tools

    assert kinds[-1] == "done"


# --------------------------------------------------------------------------- #
# Runtime: tool attribution to the assistant node (tools on)
# --------------------------------------------------------------------------- #
async def test_native_tool_events_attributed_to_assistant(db_session, monkeypatch):
    ctx = await _seed_native_ctx(db_session, enable_tools=True)
    monkeypatch.setattr("app.agents.runtime.native_runtime.ToolGateway", _FakeGateway)
    monkeypatch.setattr(
        "app.agents.runtime.native_runtime.get_provider_for_config",
        lambda cfg: _FakeProvider([
            [ChatDelta(
                tool_calls=[ToolCallDef(id="c1", name="web_search", arguments='{"query":"x"}')],
                finish_reason="tool_calls")],
            [ChatDelta(content="OK"), ChatDelta(finish_reason="stop")],
        ]),
    )

    events = await _collect(ctx)

    tool_calls = _find_all(events, "tool_call")
    assert tool_calls, "expected a tool_call event"
    assert all(d.get("agent_id") == "assistant" for d in tool_calls)
    tool_results = _find_all(events, "tool_result")
    assert all(d.get("agent_id") == "assistant" for d in tool_results)

    starts = {d["step_id"] for d in _find_all(events, "step_started")}
    assert "plan" in starts and "answer" in starts


async def test_native_rejects_oversized_tool_result_before_second_model_round(
    db_session, monkeypatch
):
    huge_content = "tool-result-item " * 10_000
    ctx = await _seed_native_ctx(db_session, enable_tools=True)
    ctx.model_config.max_context_tokens = 4_000
    ctx.model_config.max_tokens = 200
    ctx.messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "current request"},
    ]

    class HugeResultGateway(_FakeGateway):
        async def execute(self, *, tool_call_id, tool_name, arguments):
            return _FakeExecution(
                status="success",
                ok=True,
                approval_id=None,
                result={"content": huge_content},
                error=None,
                tool_call_id=tool_call_id,
                name=tool_name,
                content=huge_content,
            )

    provider = _FakeProvider(
        [
            [
                ChatDelta(
                    tool_calls=[
                        ToolCallDef(
                            id="c1", name="web_search", arguments='{"query":"x"}'
                        )
                    ],
                    finish_reason="tool_calls",
                )
            ],
            [ChatDelta(content="must not run"), ChatDelta(finish_reason="stop")],
        ]
    )
    monkeypatch.setattr("app.agents.runtime.native_runtime.ToolGateway", HugeResultGateway)
    monkeypatch.setattr(
        "app.agents.runtime.native_runtime.get_provider_for_config", lambda _cfg: provider
    )

    events = await _collect(ctx)

    assert len(provider.calls) == 1
    done = _find_all(events, "done")
    assert done[-1]["finish_reason"] == "budget"
