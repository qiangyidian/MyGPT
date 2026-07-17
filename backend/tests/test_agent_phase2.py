"""Phase 2 acceptance tests: multi-turn state, intent routing, planning, and
rolling summary.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.agents.planning import (
    build_plan,
    classify_intent,
    extract_goal,
    should_summarize,
    summarize_history,
)
from app.agents.schemas import ExecutionMode
from app.agents.state_store import load_state, save_summary, upsert_goal
from app.models import Conversation, ConversationMemory, Message, ModelConfig
from app.services.chat_service import ChatService
from app.providers.mock import MockProvider
from tests.conftest import auth_headers

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


# --------------------------------------------------------------------------- #
# Intent + planning (deterministic)
# --------------------------------------------------------------------------- #
def test_classify_intent_routes_by_keywords():
    assert classify_intent("帮我研究一下 CrewAI 和 AutoGen 的对比") == "deep_research"
    assert classify_intent("please research the latest RAG methods") == "deep_research"
    assert classify_intent("删除昨天创建的订单") == "action"
    assert classify_intent("run this sql query") == "action"
    assert classify_intent("什么是向量数据库？") == "knowledge"
    assert classify_intent("你好，今天天气不错") == "chat"


def test_build_plan_has_steps_per_intent():
    for intent in ("deep_research", "action", "knowledge", "chat"):
        summary, steps = build_plan(intent, "goal")
        assert summary
        assert isinstance(steps, list) and steps
        assert all("id" in s and "title" in s for s in steps)


def test_extract_goal_truncates():
    assert extract_goal("hello") == "hello"
    long = "x" * 500
    assert len(extract_goal(long)) == 200


# --------------------------------------------------------------------------- #
# State store round-trip
# --------------------------------------------------------------------------- #
async def test_state_store_goal_summary_roundtrip(db_session):
    conv = Conversation(user_id=_SEEDED_USER, title="state")
    db_session.add(conv)
    await db_session.flush()

    await upsert_goal(db_session, conv.id, _SEEDED_USER, "compare CrewAI vs AutoGen")
    await save_summary(db_session, conv.id, _SEEDED_USER, "用户在评估 Agent 框架")

    state = await load_state(db_session, conv.id, _SEEDED_USER)
    assert state.user_goal == "compare CrewAI vs AutoGen"
    assert "评估 Agent 框架" in state.conversation_summary

    # Upserting the goal again updates rather than duplicating.
    await upsert_goal(db_session, conv.id, _SEEDED_USER, "new goal")
    state = await load_state(db_session, conv.id, _SEEDED_USER)
    assert state.user_goal == "new goal"
    tasks = (
        await db_session.execute(
            __import__("sqlalchemy").select(ConversationMemory).where(
                ConversationMemory.conversation_id == conv.id,
                ConversationMemory.memory_type == "task",
            )
        )
    ).scalars().all()
    assert len(tasks) == 1


# --------------------------------------------------------------------------- #
# Rolling summary
# --------------------------------------------------------------------------- #
def test_should_summarize_threshold():
    assert should_summarize(800, 1000) is True   # 80% > 70%
    assert should_summarize(500, 1000) is False  # 50% < 70%
    assert should_summarize(100, 0) is False     # no budget


async def test_summarize_history_with_mock_provider():
    provider = MockProvider(base_url="http://localhost/v1", api_key="", model="mock")
    msgs = [{"role": "user", "content": f"message number {i}"} for i in range(10)]
    summary = await summarize_history(provider, msgs, keep_recent=6)
    assert summary  # mock returns a canned reply; heuristic fallback also non-empty


async def test_maybe_summarize_persists_summary(db_session):
    """A large history triggers _maybe_summarize to write a summary memory."""
    conv = Conversation(user_id=_SEEDED_USER, title="big")
    db_session.add(conv)
    await db_session.flush()
    # 8 messages, tiny context budget -> exceeds 70% -> summarize (older 2 rolled).
    for i in range(8):
        db_session.add(
            Message(
                conversation_id=conv.id,
                role="user" if i % 2 == 0 else "assistant",
                content=f"this is message number {i} with some padding text " * 20,
            )
        )
    await db_session.flush()

    cfg = ModelConfig(
        name="mock",
        provider="mock",
        api_base_url="http://localhost/v1",
        model_name="mock-model",
        max_context_tokens=100,  # tiny -> forces summarization
        max_tokens=64,
    )
    svc = ChatService()
    await svc._maybe_summarize(db_session, conv, cfg, _SEEDED_USER)

    summaries = (
        await db_session.execute(
            __import__("sqlalchemy").select(ConversationMemory).where(
                ConversationMemory.conversation_id == conv.id,
                ConversationMemory.memory_type == "summary",
            )
        )
    ).scalars().all()
    assert len(summaries) == 1


# --------------------------------------------------------------------------- #
# End-to-end: agent mode emits a plan
# --------------------------------------------------------------------------- #
async def _create_mock_model(client, headers, **overrides):
    body = {
        "name": "Mock phase2",
        "provider": "mock",
        "api_base_url": "http://localhost/v1",
        "model_name": "mock-model",
        "supports_stream": True,
        "supports_tools": False,
        "is_embedding": False,
    }
    body.update(overrides)
    r = await client.post("/api/models", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _collect_events(client, headers, body):
    events: list[tuple[str, dict]] = []
    async with client.stream("POST", "/api/chat/stream", json=body, headers=headers) as resp:
        assert resp.status_code == 200
        ev = "message"
        data_lines: list[str] = []
        async for line in resp.aiter_lines():
            line = line.rstrip()
            if line.startswith("event:"):
                ev = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
            elif line == "":
                if data_lines:
                    try:
                        events.append((ev, json.loads("\n".join(data_lines))))
                    except json.JSONDecodeError:
                        pass
                data_lines = []
                ev = "message"
    return events


async def test_agent_mode_emits_plan_created(client, monkeypatch):
    async def _fake_run(self, **kwargs):
        return {"ok": True, "query": kwargs.get("query"), "results": []}

    from app.tools.builtin import WebSearchTool

    monkeypatch.setattr(WebSearchTool, "run", _fake_run)

    h = auth_headers()
    model_id = await _create_mock_model(client, h)

    events = await _collect_events(
        client, h,
        {
            "content": "帮我研究对比一下 CrewAI 和 AutoGen 两个框架",
            "model_id": model_id,
            "enable_tools": True,
        },
    )
    kinds = [k for k, _ in events]
    assert "plan_created" in kinds, f"missing plan_created in {kinds}"
    plan = next(d for k, d in events if k == "plan_created")
    assert plan["summary"]
    assert isinstance(plan["steps"], list) and len(plan["steps"]) >= 2
    assert kinds[-1] == "done"
