"""CrewAI worker tools must use the DB owner's loop and text attribution."""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.agents.adapters.tool_adapter import _bridge_async, build_crewai_tool
from app.agents.gateway.tool_gateway import ToolGateway
from app.agents.schemas import ToolExecution
from app.agents.stage_context import make_stage_context


@pytest.mark.parametrize("entry", ["worker", "async"])
async def test_tool_gateway_runs_on_owner_loop_with_uuid_task(entry, monkeypatch):
    owner = asyncio.get_running_loop()
    task_id = uuid.uuid4()
    ctx = make_stage_context(uuid.uuid4())
    stage_token = ctx.set_stage(agent_id="kb-researcher", task_id=task_id)
    calls = []

    async def gateway(*args):
        # A loop-affine pool/driver fails here under the old asyncio.run bridge.
        assert asyncio.get_running_loop() is owner
        assert args[6:8] == ("kb-researcher", str(task_id))
        calls.append(args)
        return ToolExecution(
            ok=True, tool_call_id=args[8], tool_name="file_analyze",
            arguments={}, status="success", result={"text": "document evidence"},
        )

    monkeypatch.setattr("app.agents.adapters.tool_adapter._execute_via_gateway", gateway)
    tool = build_crewai_tool(
        SimpleNamespace(name="file_analyze", description="read document", parameters=[]),
        conversation_id=uuid.uuid4(), message_id=uuid.uuid4(), run_id=ctx.run_id,
        user_id=uuid.uuid4(), stage_ctx=ctx,
    )
    try:
        output = await asyncio.to_thread(tool._run) if entry == "worker" else await tool.arun()
    finally:
        ctx.reset_stage(stage_token)
    assert "document evidence" in output
    assert len(calls) == 1
    await asyncio.sleep(0)
    events = [ctx.queue.get_nowait(), ctx.queue.get_nowait()]
    assert [event.kind for event in events] == ["tool_call", "tool_result"]
    assert all(event.data["task_id"] == str(task_id) for event in events)


async def test_sync_bridge_rejects_blocking_owner_loop():
    async def work():
        return "done"

    with pytest.raises(RuntimeError, match="Use arun"):
        _bridge_async(work(), asyncio.get_running_loop())


def test_gateway_normalizes_uuid_attribution():
    gateway = object.__new__(ToolGateway)
    task_id = uuid.uuid4()
    gateway.set_attribution(agent_id="researcher", task_id=task_id)
    assert gateway._task_id == str(task_id)
