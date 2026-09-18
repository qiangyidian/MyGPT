"""RunEnvironment：一次 run 的共享执行环境。

装配断言 —— for_turn 必须填好 CrewAI 多 Agent 路径所需的全部字段，
否则 StreamingWriterExecutor（依赖 provider/assistant_msg）与审批桥
（依赖 stage_ctx.loop）会在运行期静默失效。
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from app.agents.run_environment import RunEnvironment
from app.agents.schemas import AgentTurnContext, ExecutionMode
from app.models import AgentRun, Conversation, Message
from tests.conftest import TestSessionLocal

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _seed_ctx(db_session) -> AgentTurnContext:
    conv = Conversation(user_id=_SEEDED_USER, title="env test")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id, message_id=msg.id, user_id=_SEEDED_USER,
        runtime="crewai", flow_name="deep_research", status="running",
    )
    db_session.add(run)
    await db_session.commit()
    cfg = SimpleNamespace(
        provider="mock", api_base_url="http://x/v1", api_key_encrypted="",
        model_name="mock", temperature=0.3, top_p=1.0, max_tokens=64,
        supports_tools=True,
    )
    user = SimpleNamespace(id=_SEEDED_USER, role="user")
    ctx = AgentTurnContext(
        db=db_session, user=user, conversation=conv, model_config=cfg,
        request=SimpleNamespace(), user_content="compare A and B",
        system_prompt="", messages=[], rag_context="", citations=[],
        assistant_msg=msg, run_id=run.id, execution_mode=ExecutionMode.agent,
        agent_profile="deep_research", enable_tools=True,
    )
    ctx.extra["persistence_session_factory"] = TestSessionLocal
    ctx.extra["persistence_lock"] = asyncio.Lock()
    return ctx


async def test_for_turn_populates_stage_context(db_session):
    ctx = await _seed_ctx(db_session)
    env = RunEnvironment.for_turn(ctx)

    assert env.run_id == ctx.run_id
    assert env.stage_ctx.run_id == ctx.run_id
    assert env.stage_ctx.model_config is ctx.model_config
    assert env.stage_ctx.assistant_msg is ctx.assistant_msg
    assert env.stage_ctx.user_content == "compare A and B"
    assert env.stage_ctx.db is ctx.db
    assert env.stage_ctx.persistence_session_factory is TestSessionLocal
    assert env.stage_ctx.persistence_lock is ctx.extra["persistence_lock"]
    # 取消事件必须存在，否则 _respect_controls 的 cancel 分支会 AttributeError。
    assert env.stage_ctx.cancel_event is not None
    # 预算守卫是同一实例，不能各建各的。
    assert env.guard is env.stage_ctx.budget_guard
    assert env.guard is not None


async def test_for_turn_installs_continuation_checkpoint(db_session):
    ctx = await _seed_ctx(db_session)
    env = RunEnvironment.for_turn(ctx)
    # 未注入时必须装好回退实现（否则长回答续写检查点静默不落库）。
    assert env.stage_ctx.persist_continuation_checkpoint is not None
    assert callable(env.stage_ctx.persist_continuation_checkpoint)


async def test_for_turn_prefers_injected_checkpoint(db_session):
    ctx = await _seed_ctx(db_session)

    async def _fake(checkpoint: dict) -> None:
        return None

    ctx.extra["persist_continuation_checkpoint"] = _fake
    env = RunEnvironment.for_turn(ctx)
    assert env.stage_ctx.persist_continuation_checkpoint is _fake


# --------------------------------------------------------------------------- #
# Task 2：生命周期、持久化与归集
# --------------------------------------------------------------------------- #
import pytest

from app.agents.graph import build_deep_research_graph
from app.agents.runtime.stage_executor import StageResult


async def _drain(env) -> list[str]:
    """取出队列里当前累积的事件 kind（先让排队的投递落地）。

    ``StageContext.emit`` 走 ``loop.call_soon_threadsafe``，投递要到下一个
    loop tick 才真正入队 —— 直接读会读到空队列。
    """
    await asyncio.sleep(0)
    kinds: list[str] = []
    while not env.stage_ctx.queue.empty():
        evt = env.stage_ctx.queue.get_nowait()
        if evt is not None:
            kinds.append(evt.kind)
    return kinds


async def _drain_events(env) -> list:
    await asyncio.sleep(0)
    out = []
    while not env.stage_ctx.queue.empty():
        evt = env.stage_ctx.queue.get_nowait()
        if evt is not None:
            out.append(evt)
    return out


async def _env_with_graph(db_session):
    ctx = await _seed_ctx(db_session)
    env = RunEnvironment.for_turn(ctx)
    env.attach_graph(build_deep_research_graph("q"))
    return env


async def test_begin_emits_graph_and_running_status(db_session):
    env = await _env_with_graph(db_session)
    env.begin()
    kinds = await _drain(env)
    assert kinds[0] == "agent_graph"
    assert kinds[1] == "run_status"
    # emit_run_status("running") 只写 graph.status；运行的守护由图自身的
    # recompute_active() 负责，不在这里断言节点态。
    assert env.emitter.graph.status == "running"


async def test_attach_graph_builds_approval_bridge(db_session):
    env = await _env_with_graph(db_session)
    assert env.stage_ctx.approval_bridge is not None
    assert str(env.emitter.graph.run_id) == str(env.run_id)

async def test_emitter_before_attach_raises(db_session):
    ctx = await _seed_ctx(db_session)
    env = RunEnvironment.for_turn(ctx)
    with pytest.raises(RuntimeError):
        _ = env.emitter


async def test_step_lifecycle_transitions_are_honest(db_session):
    env = await _env_with_graph(db_session)
    env.begin()
    await _drain(env)

    assert env.step_started("researcher", title="检索") is True
    assert env.step_completed("researcher", output="证据", output_summary="证据") is None
    node = env.emitter.graph.node("researcher")
    assert node.status.value == "completed"
    assert node.duration_ms is not None

    # 下游 join 未满足前不得 running —— 由 emitter 守卫保证。
    assert env.step_started("writer") is False


async def test_step_failed_cancels_downstream(db_session):
    env = await _env_with_graph(db_session)
    env.begin()
    await _drain(env)
    env.step_started("researcher")
    env.step_failed("researcher", error="boom")

    assert env.emitter.graph.node("researcher").status.value == "failed"
    assert env.emitter.graph.node("analyst").status.value == "cancelled"
    assert env.emitter.graph.node("writer").status.value == "cancelled"


async def test_step_completed_charges_guard_once(db_session):
    env = await _env_with_graph(db_session)
    env.begin()
    await _drain(env)
    env.step_started("researcher")
    env.step_completed(
        "researcher", output="x", usage={"total_tokens": 10, "cost_usd": 0.5}
    )
    snapshot = env.guard.snapshot()
    assert snapshot["tokens_used"] == 10

    # usage_charged=True 的表征调用方已实时计费，不得重复累加。
    env.step_started("analyst")
    env.step_completed(
        "analyst", output="y",
        usage={"total_tokens": 7, "cost_usd": 0.2}, usage_charged=True,
    )
    assert env.guard.snapshot()["tokens_used"] == 10


async def test_finish_sets_terminal_status(db_session):
    env = await _env_with_graph(db_session)
    env.begin()
    await _drain(env)
    env.finish("completed")
    assert env.emitter.graph.status == "completed"


async def test_persist_graph_writes_run_row(db_session):
    env = await _env_with_graph(db_session)
    env.begin()
    await env.persist_graph(definition=True)

    from sqlalchemy import select
    from app.models import AgentRun

    row = (
        await db_session.execute(select(AgentRun).where(AgentRun.id == env.ctx.run_id))
    ).scalar_one()
    await db_session.refresh(row)
    assert row.graph_state is not None
    assert row.graph_definition is not None


async def test_aggregate_usage_includes_charged_stages(db_session):
    env = await _env_with_graph(db_session)
    results = {
        "researcher": StageResult(
            agent_id="researcher", raw="a",
            usage={"total_tokens": 5, "cost_usd": 0.1}, usage_charged=False,
        ),
        "analyst": StageResult(
            agent_id="analyst", raw="b",
            usage={"total_tokens": 3, "cost_usd": 0.05}, usage_charged=True,
        ),
    }
    agg = env.aggregate_usage(results)
    assert agg is not None
    assert agg["total_tokens"] == 8
