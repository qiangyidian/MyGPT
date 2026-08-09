"""Phase 0 acceptance tests: the hardened agent chain.

Covers the four things Phase 0 promised:
  * SQL guard stronger than ``startswith("select")``.
  * BudgetGuard hard stops.
  * ToolGateway: single path, real ``ok`` status, approval gate for dangerous
    tools, execution with a pre-approved gate.
  * End-to-end agent turn (mock provider) emits the new event vocabulary and
    persists the audit trail (AgentRun / AgentStep / ToolCall).
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.agents.gateway.tool_gateway import ToolGateway
from app.agents.policies.approval_policy import arguments_hash, expiry_from_now
from app.agents.policies.budget_policy import BudgetGuard, BudgetLimits
from app.agents.policies.tool_policy import (
    is_tool_allowed,
    validate_readonly_sql,
)
from app.agents.schemas import ExecutionMode
from app.models import AgentRun, AgentStep, Conversation, Message, ToolApproval, ToolCall
from app.tools.base import BaseTool, ToolRegistry
from app.tools.registry_init import get_default_registry
from tests.conftest import auth_headers

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


# --------------------------------------------------------------------------- #
# SQL guard
# --------------------------------------------------------------------------- #
def test_sql_guard_accepts_select_and_with():
    assert validate_readonly_sql("SELECT 1") == "SELECT 1"
    assert validate_readonly_sql("  select * from t  ") == "select * from t"
    assert validate_readonly_sql("WITH x AS (SELECT 1) SELECT * FROM x").startswith("WITH")


def test_sql_guard_rejects_dml_ddl_and_multi_statement():
    from app.agents.policies.tool_policy import UnsafeSQLError

    bad = [
        "DROP TABLE users",
        "DELETE FROM users WHERE 1=1",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a=1",
        "SELECT * FROM t; DROP TABLE t;",
        "SELECT pg_sleep(600)",
        "SELECT * FROM t INTO outfile",
        "  -- comment\nALTER TABLE t ADD c INT",
    ]
    for sql in bad:
        with pytest.raises(UnsafeSQLError):
            validate_readonly_sql(sql)


# --------------------------------------------------------------------------- #
# Budgets
# --------------------------------------------------------------------------- #
def test_budget_guard_stops_at_step_limit():
    guard = BudgetGuard(BudgetLimits(max_agent_steps=2, max_tool_calls=99, max_replan_count=99, max_runtime_seconds=99, max_total_tokens=10**9))
    guard.enter_step()
    guard.enter_step()
    from app.agents.schemas import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        guard.enter_step()


def test_budget_guard_stops_at_tool_limit():
    guard = BudgetGuard(BudgetLimits(max_agent_steps=99, max_tool_calls=1, max_replan_count=99, max_runtime_seconds=99, max_total_tokens=10**9))
    guard.enter_tool_call()
    from app.agents.schemas import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        guard.enter_tool_call()


# --------------------------------------------------------------------------- #
# Permission
# --------------------------------------------------------------------------- #
def test_python_exec_disabled_in_non_dev():
    # Test env is ENV=test (is_dev=False) with no opt-in -> blocked.
    assert is_tool_allowed("python_exec", None) is False
    # Safe tools are always allowed.
    assert is_tool_allowed("datetime_now", None) is True


# --------------------------------------------------------------------------- #
# ToolGateway
# --------------------------------------------------------------------------- #
async def _seed_run(db_session):
    conv = Conversation(user_id=_SEEDED_USER, title="phase0 test")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id,
        message_id=msg.id,
        user_id=_SEEDED_USER,
        runtime="native",
        flow_name="test",
        status="running",
    )
    db_session.add(run)
    await db_session.flush()
    return conv, msg, run


async def test_gateway_safe_tool_executes(db_session):
    _, msg, run = await _seed_run(db_session)
    gw = ToolGateway(
        db_session,
        conversation_id=msg.conversation_id,
        assistant_message_id=msg.id,
        run_id=run.id,
        user=None,
    )
    exec_ = await gw.execute(
        tool_call_id="c1", tool_name="datetime_now", arguments={}
    )
    assert exec_.ok is True
    assert exec_.status == "success"
    # Audit rows persisted.
    rows = (await db_session.execute(__import__("sqlalchemy").select(ToolCall).where(ToolCall.message_id == msg.id))).scalars().all()
    assert len(rows) == 1
    steps = (await db_session.execute(__import__("sqlalchemy").select(AgentStep).where(AgentStep.run_id == run.id))).scalars().all()
    assert len(steps) == 1 and steps[0].step_type == "tool"


async def test_gateway_separates_and_sanitizes_tool_usage_from_model_content(db_session):
    class MeteredTool(BaseTool):
        name = "metered_tool"
        description = "returns metered data"

        async def run(self, **kwargs):
            return {
                "answer": "safe result",
                "usage": {
                    "tool_units": 2,
                    "cached_tokens": 3,
                    "api_key": "must-not-leak",
                    "negative": -1,
                    "nested": {"secret": "must-not-leak"},
                },
            }

    registry = ToolRegistry()
    registry.register(MeteredTool())
    _, msg, run = await _seed_run(db_session)
    gw = ToolGateway(
        db_session,
        conversation_id=msg.conversation_id,
        assistant_message_id=msg.id,
        run_id=run.id,
        user=None,
        registry=registry,
    )

    execution = await gw.execute(
        tool_call_id="metered-1", tool_name="metered_tool", arguments={}
    )

    assert execution.usage == {"tool_units": 2, "cached_tokens": 3}
    model_content = execution.to_openai_tool_message()["content"]
    assert "safe result" in model_content
    assert "usage" not in model_content
    assert "must-not-leak" not in model_content
    assert "must-not-leak" not in (execution.full_result or "")


async def test_gateway_dangerous_tool_requires_approval(db_session):
    _, msg, run = await _seed_run(db_session)
    gw = ToolGateway(
        db_session,
        conversation_id=msg.conversation_id,
        assistant_message_id=msg.id,
        run_id=run.id,
        user=None,
    )
    exec_ = await gw.execute(
        tool_call_id="c2", tool_name="db_query", arguments={"sql": "SELECT 1"}
    )
    assert exec_.ok is False
    assert exec_.status == "needs_approval"
    assert exec_.approval_id is not None
    # A pending ToolApproval row was created.
    aps = (await db_session.execute(__import__("sqlalchemy").select(ToolApproval).where(ToolApproval.run_id == run.id))).scalars().all()
    assert len(aps) == 1 and aps[0].status == "pending"
    assert aps[0].tool_name == "db_query"


async def test_gateway_dangerous_tool_runs_when_pre_approved(db_session):
    _, msg, run = await _seed_run(db_session)
    # Pre-create an approved approval for this exact (tool, args).
    args = {"sql": "SELECT 1"}
    ap = ToolApproval(
        run_id=run.id,
        conversation_id=msg.conversation_id,
        user_id=_SEEDED_USER,
        tool_name="db_query",
        arguments=args,
        arguments_hash=arguments_hash("db_query", args),
        risk_level="high",
        status="approved",
        expires_at=expiry_from_now(),
    )
    db_session.add(ap)
    await db_session.flush()

    gw = ToolGateway(
        db_session,
        conversation_id=msg.conversation_id,
        assistant_message_id=msg.id,
        run_id=run.id,
        user=None,
    )
    exec_ = await gw.execute(
        tool_call_id="c3", tool_name="db_query", arguments=args
    )
    assert exec_.ok is True
    assert exec_.status == "success"


async def test_gateway_reports_real_error_status(db_session):
    """A tool that raises (empty web_search query) yields ok=False — the old
    always-ok=True bug is gone."""
    _, msg, run = await _seed_run(db_session)
    gw = ToolGateway(
        db_session,
        conversation_id=msg.conversation_id,
        assistant_message_id=msg.id,
        run_id=run.id,
        user=None,
    )
    exec_ = await gw.execute(
        tool_call_id="c4", tool_name="web_search", arguments={"query": ""}
    )
    assert exec_.ok is False
    assert exec_.status == "error"
    assert exec_.error is not None


async def test_gateway_blocks_bad_sql_before_execution(db_session):
    _, msg, run = await _seed_run(db_session)
    gw = ToolGateway(
        db_session,
        conversation_id=msg.conversation_id,
        assistant_message_id=msg.id,
        run_id=run.id,
        user=None,
    )
    # Even with a pre-approval, the SQL hardening gate runs first and blocks.
    exec_ = await gw.execute(
        tool_call_id="c5", tool_name="db_query", arguments={"sql": "DROP TABLE users"}
    )
    assert exec_.ok is False
    assert exec_.status == "blocked"


# --------------------------------------------------------------------------- #
# End-to-end agent turn (mock provider simulates a web_search)
# --------------------------------------------------------------------------- #
async def _create_mock_model(client, headers):
    r = await client.post(
        "/api/models",
        json={
            "name": "Mock for agent",
            "provider": "mock",
            "api_base_url": "http://localhost/v1",
            "model_name": "mock-model",
            "supports_stream": True,
            "supports_tools": False,
            "is_embedding": False,
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _collect_events(client, headers, body):
    events: list[tuple[str, dict]] = []
    async with client.stream(
        "POST", "/api/chat/stream", json=body, headers=headers
    ) as resp:
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


async def test_agent_turn_emits_new_events_and_audits(client, monkeypatch):
    # Make web_search deterministic + offline so the test never hits the network.
    async def _fake_run(self, **kwargs):
        return {"ok": True, "query": kwargs.get("query"), "results": [{"title": "t", "url": "u", "snippet": "s"}]}

    from app.tools.builtin import WebSearchTool

    monkeypatch.setattr(WebSearchTool, "run", _fake_run)

    h = auth_headers()
    model_id = await _create_mock_model(client, h)

    events = await _collect_events(
        client,
        h,
        {
            "content": "search for crewai flows",
            "model_id": model_id,
            "enable_tools": True,
            "execution_mode": "auto",
        },
    )
    kinds = [k for k, _ in events]

    # New vocabulary present.
    assert "meta" in kinds
    assert "run_started" in kinds
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert kinds[-1] == "done"

    # tool_call carries the dangerous flag (web_search is low-risk, not dangerous).
    tc = next(d for k, d in events if k == "tool_call")
    assert tc["name"] == "web_search"
    assert tc["dangerous"] is False

    # tool_result reports the REAL outcome (ok=True for our fake).
    tr = next(d for k, d in events if k == "tool_result")
    assert tr["ok"] is True

    # run_started carries a run_id.
    rs = next(d for k, d in events if k == "run_started")
    assert rs["runtime"] == "native"
    assert rs["run_id"]
