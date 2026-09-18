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
