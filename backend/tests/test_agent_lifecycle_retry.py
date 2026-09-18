"""重试 × 终态守卫：引擎的 transient 重试不得留下矛盾状态。

背景：WorkflowEngine 的 RetryPolicy 允许 transient 失败后重试。旧实现里
attempt1 失败会把节点置为终态 failed，attempt2 成功又把 status 翻回
completed，但 error 没清、duration_ms 归零 —— 用户看到「已完成、带错误、
耗时 0ms」的节点。

说明：`make_stage_context` 需要运行中的事件循环，且 `StageContext.emit` 走
`loop.call_soon_threadsafe`（回调要等循环下一轮才落到队列），所以用例都是
async 并在断言前 `await asyncio.sleep(0)` —— 与 tests/test_agent_events.py、
tests/test_streaming_writer.py 的既有写法一致。
"""
from __future__ import annotations

import asyncio
import uuid

from app.agents.graph import AgentNodeStatus, build_deep_research_graph
from app.agents.lifecycle import AgentLifecycleEmitter
from app.agents.stage_context import make_stage_context


async def _emitter():
    stage_ctx = make_stage_context(str(uuid.uuid4()))
    graph = build_deep_research_graph("q")
    emitter = AgentLifecycleEmitter(
        run_id=uuid.UUID(stage_ctx.run_id), graph=graph, stage_ctx=stage_ctx
    )
    emitter.emit_graph_initialized()
    return emitter, graph


async def _drain(stage_ctx) -> list[str]:
    """让已排队的 emit 回调落到队列后清空，返回被丢弃的事件 kind。"""
    await asyncio.sleep(0)
    kinds: list[str] = []
    while not stage_ctx.queue.empty():
        evt = stage_ctx.queue.get_nowait()
        if evt is not None:
            kinds.append(evt.kind)
    return kinds


async def _take_events(stage_ctx) -> list:
    await asyncio.sleep(0)
    events = []
    while not stage_ctx.queue.empty():
        evt = stage_ctx.queue.get_nowait()
        if evt is not None:
            events.append(evt)
    return events


async def test_success_after_failure_clears_error_and_keeps_duration():
    emitter, graph = await _emitter()
    emitter.emit_agent_started("researcher")
    emitter.emit_agent_failed("researcher", error="Connection error.")
    assert graph.node("researcher").status == AgentNodeStatus.failed

    emitter.emit_agent_completed("researcher", output_summary="重试成功")

    node = graph.node("researcher")
    assert node.status == AgentNodeStatus.completed
    # 重试成功后失败信息必须消失。
    assert node.error is None
    # 耗时是从首次开始累计的，不能归零。
    assert node.duration_ms is not None
    assert node.duration_ms >= 0


async def test_emit_agent_retrying_flips_failed_back_to_running():
    emitter, graph = await _emitter()
    emitter.emit_agent_started("researcher")
    emitter.emit_agent_failed("researcher", error="timeout")

    emitter.emit_agent_retrying("researcher", attempt=2, error="timeout")

    node = graph.node("researcher")
    assert node.status == AgentNodeStatus.running
    assert node.retrying == {"attempt": 2, "error": "timeout"}


async def test_retrying_emits_agent_status_event():
    emitter, _graph = await _emitter()
    stage_ctx = emitter.ctx
    emitter.emit_agent_started("researcher")
    emitter.emit_agent_failed("researcher", error="timeout")
    await _drain(stage_ctx)

    emitter.emit_agent_retrying("researcher", attempt=2, error="timeout")

    events = await _take_events(stage_ctx)
    status_events = [e for e in events if e.kind == "agent_status"]
    assert status_events, "retrying 必须发 agent_status"
    assert status_events[0].data["status"] == "running"
    assert status_events[0].data["retrying"] == {"attempt": 2, "error": "timeout"}


async def test_completed_clears_retrying_marker():
    emitter, graph = await _emitter()
    emitter.emit_agent_started("researcher")
    emitter.emit_agent_failed("researcher", error="timeout")
    emitter.emit_agent_retrying("researcher", attempt=2, error="timeout")

    emitter.emit_agent_completed("researcher", output_summary="ok")

    assert graph.node("researcher").retrying is None


async def test_cancelled_is_not_overwritten_by_completed():
    emitter, graph = await _emitter()
    emitter.emit_agent_started("researcher")
    emitter.emit_agent_cancelled("researcher")

    emitter.emit_agent_completed("researcher", output_summary="迟到")

    # 用户主动取消是真正的终态，不得被翻回 completed。
    assert graph.node("researcher").status == AgentNodeStatus.cancelled


async def test_retrying_ignored_for_cancelled_node():
    emitter, graph = await _emitter()
    emitter.emit_agent_started("researcher")
    emitter.emit_agent_cancelled("researcher")

    emitter.emit_agent_retrying("researcher", attempt=2, error="x")

    assert graph.node("researcher").status == AgentNodeStatus.cancelled
    assert graph.node("researcher").retrying is None
