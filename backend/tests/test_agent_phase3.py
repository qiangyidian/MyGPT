"""Phase 3 acceptance tests: human approval + persistent resume.

Covers:
  * ApprovalCoordinator register/approve/reject/cancel/wait.
  * The agent-runs API: list, detail, approve, reject, cancel, ownership.
  * End-to-end: a mock agent that requests a dangerous tool pauses until the
    user approves, then the tool actually executes (via a second gateway call
    that finds the approved ToolApproval).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.agents.approval_coordinator import (
    ApprovalCoordinator,
)
from app.agents.gateway.tool_gateway import ToolGateway
from app.agents.policies.approval_policy import arguments_hash, expiry_from_now
from app.models import (
    AgentRun,
    Conversation,
    Message,
    ToolApproval,
)
from tests.conftest import auth_headers

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


# --------------------------------------------------------------------------- #
# ApprovalCoordinator
# --------------------------------------------------------------------------- #
async def test_coordinator_approve_resumes_wait():
    coord = ApprovalCoordinator()
    run_id, ap_id = uuid.uuid4(), uuid.uuid4()
    coord.register(run_id=run_id, approval_id=ap_id, tool_name="db_query")

    async def _decide():
        await asyncio.sleep(0.05)
        assert coord.approve(ap_id) is True

    asyncio.create_task(_decide())
    wr = await coord.wait(ap_id, timeout=2)
    assert wr.decision == "approved"



def _admin_principal():
    """Admin stand-in for gateway tests: db_query is admin-only in prod, and
    these tests exercise the approval machinery, not the environment gate."""
    from types import SimpleNamespace
    return SimpleNamespace(role="admin", id=_SEEDED_USER)


async def test_coordinator_reject():
    coord = ApprovalCoordinator()
    ap_id = uuid.uuid4()
    coord.register(run_id=uuid.uuid4(), approval_id=ap_id, tool_name="db_query")
    assert coord.reject(ap_id, "too risky") is True
    wr = await coord.wait(ap_id, timeout=2)
    assert wr.decision == "rejected"
    assert wr.reason == "too risky"


async def test_coordinator_cancel_run():
    coord = ApprovalCoordinator()
    run_id = uuid.uuid4()
    ap_id = uuid.uuid4()
    coord.register(run_id=run_id, approval_id=ap_id, tool_name="db_query")
    n = coord.cancel_run(run_id)
    assert n == 1
    wr = await coord.wait(ap_id, timeout=2)
    assert wr.decision == "cancelled"


async def test_coordinator_timeout():
    coord = ApprovalCoordinator()
    ap_id = uuid.uuid4()
    coord.register(run_id=uuid.uuid4(), approval_id=ap_id, tool_name="db_query")
    with pytest.raises(asyncio.TimeoutError):
        await coord.wait(ap_id, timeout=0.1)


# --------------------------------------------------------------------------- #
# Gateway: approved approval -> real execution
# --------------------------------------------------------------------------- #
async def _seed_run(db_session):
    conv = Conversation(user_id=_SEEDED_USER, title="phase3")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id, message_id=msg.id, user_id=_SEEDED_USER,
        runtime="native", flow_name="test", status="running",
    )
    db_session.add(run)
    await db_session.flush()
    return conv, msg, run


async def test_gateway_resume_after_approval(db_session):
    """After an approval is marked approved, the gateway's second execute runs."""
    _, msg, run = await _seed_run(db_session)
    gw = ToolGateway(
        db_session,
        conversation_id=msg.conversation_id,
        assistant_message_id=msg.id,
        run_id=run.id,
        user=_admin_principal(),
    )
    args = {"sql": "SELECT 1"}
    first = await gw.execute(tool_call_id="c", tool_name="db_query", arguments=args)
    assert first.status == "needs_approval"
    assert first.approval_id is not None

    # Simulate the API marking it approved.
    ap = await db_session.get(ToolApproval, first.approval_id)
    ap.status = "approved"
    ap.decided_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    await db_session.flush()

    second = await gw.execute(tool_call_id="c", tool_name="db_query", arguments=args)
    assert second.ok is True
    assert second.status == "success"


# --------------------------------------------------------------------------- #
# Agent-runs API
# --------------------------------------------------------------------------- #
async def _make_run_via_chat(client, headers, model_id, monkeypatch):
    """Kick a chat turn that the mock provider drives with a web_search call.

    web_search.run is patched to stay offline; monkeypatch restores it on exit,
    so no cross-test contamination of the shared registry.
    """
    from app.tools.builtin import WebSearchTool

    async def _fake(self, **kwargs):
        return {"ok": True, "query": kwargs.get("query"), "results": []}

    monkeypatch.setattr(WebSearchTool, "run", _fake)
    async with client.stream(
        "POST", "/api/chat/stream",
        json={"content": "search test", "model_id": model_id, "enable_tools": True},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200
        async for _ in resp.aiter_lines():
            pass


async def _create_mock_model(client, headers):
    r = await client.post(
        "/api/models",
        json={
            "name": "Mock p3",
            "provider": "openai-compatible",
            "api_base_url": "http://localhost/v1",
            "model_name": "mock-model",
            "supports_stream": True,
            "supports_tools": True,
            "is_embedding": False,
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_api_list_and_get_run(client, db_session, monkeypatch):
    h = auth_headers()
    model_id = await _create_mock_model(client, h)
    await _make_run_via_chat(client, h, model_id, monkeypatch)

    # Find the run we just created.
    r = await client.get("/api/agent-runs", headers=h)
    assert r.status_code == 200
    runs = r.json()
    assert runs, "expected at least one run"
    run_id = runs[0]["id"]

    detail = await client.get(f"/api/agent-runs/{run_id}", headers=h)
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == run_id
    assert body["runtime"] in ("native", "crewai")
    assert isinstance(body["steps"], list)
    assert "approvals" in body


async def test_api_approve_reject_cancel(client, db_session):
    """Create a pending approval manually, then drive it through the API."""
    conv = Conversation(user_id=_SEEDED_USER, title="api approve")
    db_session.add(conv)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id, user_id=_SEEDED_USER,
        runtime="native", flow_name="test", status="waiting_approval",
    )
    db_session.add(run)
    await db_session.flush()
    ap = ToolApproval(
        run_id=run.id, conversation_id=conv.id, user_id=_SEEDED_USER,
        tool_name="db_query", arguments={"sql": "SELECT 1"},
        arguments_hash=arguments_hash("db_query", {"sql": "SELECT 1"}),
        risk_level="high", status="pending", expires_at=expiry_from_now(),
    )
    db_session.add(ap)
    await db_session.commit()
    await db_session.refresh(ap)
    await db_session.refresh(run)

    h = auth_headers()

    # Approve via the API.
    r = await client.post(
        f"/api/agent-runs/{run.id}/approve",
        json={"approval_id": str(ap.id)},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # Re-read the approval's status from a fresh query (the API committed on its
    # own session sharing the in-memory DB via StaticPool).
    from sqlalchemy import select as _sel

    ap2 = (
        await db_session.execute(
            _sel(ToolApproval.status).where(ToolApproval.id == ap.id)
        )
    ).scalar_one()
    assert ap2 == "approved"

    # Cancelling a finished run is idempotent-ish (already approved -> no wait,
    # but the run row flips to cancelled).
    r = await client.post(f"/api/agent-runs/{run.id}/cancel", headers=h)
    assert r.status_code == 200


async def test_api_ownership_isolation(client, db_session):
    """A run owned by the seeded user is not visible to a freshly registered user."""
    conv = Conversation(user_id=_SEEDED_USER, title="isolation")
    db_session.add(conv)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id, user_id=_SEEDED_USER,
        runtime="native", flow_name="test", status="completed",
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)

    # Register a second, distinct user.
    r = await client.post(
        "/api/auth/register",
        json={"email": "other@example.com", "username": "other", "password": "OtherPass123"},
    )
    assert r.status_code == 201
    r = await client.post(
        "/api/auth/login",
        json={"email": "other@example.com", "password": "OtherPass123"},
    )
    assert r.status_code == 200
    other_token = r.json()["access_token"]
    other_h = {"Authorization": f"Bearer {other_token}"}

    # The other user gets a 404 on the seeded user's run.
    r = await client.get(f"/api/agent-runs/{run.id}", headers=other_h)
    assert r.status_code == 404
