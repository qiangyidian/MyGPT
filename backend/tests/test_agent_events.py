"""新事件契约：step_output / step_progress + agent_status 的 usage 扩展。"""
from __future__ import annotations

import uuid

from app.agents.graph import build_deep_research_graph
from app.agents.lifecycle import AgentLifecycleEmitter
from app.agents.schemas import ev_agent_status, ev_step_output, ev_step_progress
from app.agents.stage_context import make_stage_context


def test_ev_step_output_payload():
    run_id = uuid.uuid4()
    evt = ev_step_output(
        run_id=run_id, agent_id="researcher", text="证据正文",
        truncated=False, chars=4,
    )
    assert evt.kind == "step_output"
    assert evt.data["run_id"] == str(run_id)
    assert evt.data["agent_id"] == "researcher"
    assert evt.data["text"] == "证据正文"
    assert evt.data["truncated"] is False
    assert evt.data["chars"] == 4


def test_ev_step_output_flags_truncation():
    evt = ev_step_output(
        run_id=uuid.uuid4(), agent_id="a", text="x" * 10, truncated=True, chars=99
    )
    assert evt.data["truncated"] is True
    assert evt.data["chars"] == 99


def test_ev_step_progress_payload_omits_missing_note():
    evt = ev_step_progress(run_id=uuid.uuid4(), agent_id="researcher", elapsed_s=12.5)
    assert evt.kind == "step_progress"
    assert evt.data["elapsed_s"] == 12.5
    assert "note" not in evt.data


def test_ev_step_progress_includes_note():
    evt = ev_step_progress(
        run_id=uuid.uuid4(), agent_id="r", elapsed_s=1.0, note="最近工具：web_search"
    )
    assert evt.data["note"] == "最近工具：web_search"


def test_ev_agent_status_carries_usage_and_cost():
    evt = ev_agent_status(
        run_id=uuid.uuid4(), agent_id="researcher", status="completed",
        usage={"total_tokens": 42}, cost_usd=0.03,
    )
    assert evt.data["usage"] == {"total_tokens": 42}
    assert evt.data["cost_usd"] == 0.03


def test_ev_agent_status_omits_usage_when_absent():
    evt = ev_agent_status(run_id=uuid.uuid4(), agent_id="r", status="running")
    assert "usage" not in evt.data
    assert "cost_usd" not in evt.data


async def test_emitter_records_usage_on_node():
    stage_ctx = make_stage_context(str(uuid.uuid4()))
    graph = build_deep_research_graph("q")
    emitter = AgentLifecycleEmitter(
        run_id=uuid.UUID(stage_ctx.run_id), graph=graph, stage_ctx=stage_ctx
    )
    emitter.emit_agent_started("researcher")
    emitter.emit_agent_completed(
        "researcher", output_summary="摘要",
        usage={"total_tokens": 42}, cost_usd=0.03,
    )
    node = graph.node("researcher")
    assert node.usage == {"total_tokens": 42}
    assert node.cost_usd == 0.03


def test_rich_step_events_setting_defaults_on():
    from app.core.config import get_settings

    assert get_settings().AGENT_RICH_STEP_EVENTS is True
    assert get_settings().AGENT_STEP_PROGRESS_INTERVAL_S == 5.0
