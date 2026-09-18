# 多 Agent 运行环境统一 + 过程可见性 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 抽出共享的 `RunEnvironment`，让 CrewAI walker 与 WorkflowEngine 用同一套执行环境与事件词汇；引擎路径补齐至同级（审批桥、预算、工具归属、usage 归集、graph_state 落库、拓扑来自 plan）；落地中间产出全文可见 + 运行中心跳 + 节点耗时/tokens/成本。

**Architecture:** `RunEnvironment` 持有而非继承已有的 `StageContext` 与 `AgentLifecycleEmitter`——那两个类保持原样，本类只收归「谁在什么时候调它们」。`CrewAIRuntime` 改为委托它（行为等价重构，靠既有测试保证）；`orchestrator._run_engine_path` 也改用它，从而拿到全部运行时能力。新增事件为纯增量，老客户端忽略。

**Tech Stack:** Python 3.13 / FastAPI / pydantic v2 / asyncio / CrewAI（懒加载）/ pytest；前端 Next.js + TypeScript + zustand + React Query。

**Spec:** `docs/superpowers/specs/2026-09-18-agent-run-environment-design.md`

## Global Constraints

- **语言**：面向用户文案用中文；注释/文档允许全角标点（`pyproject.toml` 已关掉 `RUF001/RUF002/RUF003`，那不是笔误）。
- **后端门禁**：`ruff check app tests` 必须全绿。仓库**只跑 `ruff check`，不跑 `ruff format`**（有历史格式债，全量 reformat 会冲垮 diff）。ruff 在 CI 钉死为 `0.15.17`。
- **测试命令**（在 `backend/` 下）：
  ```
  pytest tests -q --tb=short \
    --deselect tests/test_agent_phase2.py::test_agent_mode_emits_plan_created \
    --deselect tests/test_agent_phase5.py::test_full_native_agent_path
  ```
  上面两个 deselect 是打真实模型端点的网络依赖用例。本地验证时若遇到 `test_durable_controls.py::test_multi_agent_approval_pauses_then_resumes` 死锁（已知偶发，会级联一批 `OperationalError`），把它一并 deselect。
- **前端门禁**（在 `frontend/` 下）：`npm run typecheck && npm run lint && npm run test`
- **零迁移**：本计划不新增数据库表、不新增 `backend/migrations/versions/` 文件。若实施中意外需要迁移，**推 main 前必须另跑 `./scripts/verify_migrations.sh`**。
- **不推送**：本计划的提交全部留在本地。push 到 `main` 会触发生产自动部署（`deploy.yml` + `mychat-deploy.timer`）。是否推送由用户决定。
- **远端名是 `MyGPT`**，不是 `origin`。
- **默认值**：`AGENT_RICH_STEP_EVENTS` 默认 `True`；`AGENT_WORKFLOW_ENGINE` 保持默认 `""`（关）。
- **不做**（spec §2 非目标）：不迁移任何 profile 到引擎、不做非 writer 的逐 token 流式、不打开计划审批门、不给辩论加 UI 入口、不引入 LLM 规划器/verifier/新拓扑。

---

### Task 1: `RunEnvironment` 骨架与构造器

**Files:**
- Create: `backend/app/agents/run_environment.py`
- Test: `backend/tests/test_run_environment.py`

**Interfaces:**
- Consumes: `AgentTurnContext`（`app.agents.schemas`）、`StageContext`/`make_stage_context`（`app.agents.stage_context`）、`_guard_for_context`（`app.agents.runtime.crewai_runtime`）、`AsyncSessionLocal`（`app.db`）
- Produces:
  - `RunEnvironment` dataclass，字段 `run_id: str`、`ctx: AgentTurnContext`、`stage_ctx: StageContext`、`guard: BudgetGuard`
  - `RunEnvironment.for_turn(ctx: AgentTurnContext) -> RunEnvironment`
  - `RunEnvironment.attach_graph(graph: AgentGraph) -> AgentLifecycleEmitter`（Task 2 实现）
  - `RunEnvironment.begin() -> None`（Task 2 实现）

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_run_environment.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && pytest tests/test_run_environment.py -q --tb=short
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agents.run_environment'`

- [ ] **Step 3: 写最小实现**

创建 `backend/app/agents/run_environment.py`：

```python
"""RunEnvironment：一次 run 的共享执行环境。

两个 walker —— :class:`~app.agents.runtime.crewai_runtime.CrewAIRuntime` 的
静态 stage walker 与 :class:`~app.agents.workflow.engine.WorkflowEngine` 的
DAG 调度器 —— 共用本类作为唯一入口。

它**持有**而非继承已有的 :class:`~app.agents.stage_context.StageContext` 与
:class:`~app.agents.lifecycle.AgentLifecycleEmitter`；那两个类保持原样。
本类的职责只是收归「谁在什么时候调它们」——在此之前，这套装配逻辑内联在
``CrewAIRuntime._run_multi_agent`` 里，导致引擎路径只能拿到一个裸的
StageContext，从而缺失审批桥、流式字段、工具归属与 usage 归集。

命名说明：``app/agents/environments.py`` 是 Codex 风格的 workspace 环境
（cwd / shell / ready 状态），与本类无关。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.agents.graph import AgentGraph
from app.agents.lifecycle import AgentLifecycleEmitter
from app.agents.schemas import AgentTurnContext
from app.agents.stage_context import StageContext, make_stage_context
from app.db import AsyncSessionLocal

logger = logging.getLogger(__name__)


@dataclass
class RunEnvironment:
    """一次 run 的共享执行环境。"""

    run_id: str
    ctx: AgentTurnContext
    stage_ctx: StageContext
    guard: Any = None
    _emitter: AgentLifecycleEmitter | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ #
    @classmethod
    def for_turn(cls, ctx: AgentTurnContext) -> RunEnvironment:
        """装配 stage_ctx。此时 graph 尚未构建（它依赖 tools，tools 依赖
        stage_ctx），emitter 与审批桥由 :meth:`attach_graph` 建。"""
        from app.agents.runtime.crewai_runtime import _guard_for_context

        guard = _guard_for_context(ctx)
        stage_ctx = make_stage_context(ctx.run_id, budget_guard=guard)

        # 流式 writer 字段：writer stage 直接调 provider 并增量改写助手消息。
        # 全部 Optional；对非 writer stage 与 fake/demo 无副作用。
        try:
            # 会话 id 同时充当 provider 的 session identity（OpenCode 网关要求
            # 每会话稳定；Hermes 用它划定服务端记忆范围）。与 native runtime 同约。
            from app.providers.registry import get_provider_for_config

            stage_ctx.provider = get_provider_for_config(
                ctx.model_config, session_id=str(ctx.conversation.id)
            )
        except TypeError:
            # 注入的测试替身可能仍是单参签名。
            try:
                from app.providers.registry import get_provider_for_config

                stage_ctx.provider = get_provider_for_config(ctx.model_config)
            except Exception as exc:
                logger.warning("could not build provider for streaming writer: %s", exc)
                stage_ctx.provider = None
        except Exception as exc:
            logger.warning("could not build provider for streaming writer: %s", exc)
            stage_ctx.provider = None

        stage_ctx.model_config = ctx.model_config
        stage_ctx.assistant_msg = ctx.assistant_msg
        stage_ctx.user_content = ctx.user_content
        stage_ctx.cancel_event = asyncio.Event()
        stage_ctx.db = ctx.db
        stage_ctx.persistence_session_factory = (
            ctx.extra.get("persistence_session_factory") or AsyncSessionLocal
        )
        stage_ctx.persistence_lock = ctx.extra.get("persistence_lock")
        stage_ctx.persist_continuation_checkpoint = cls._resolve_checkpoint(ctx)
        return cls(run_id=ctx.run_id, ctx=ctx, stage_ctx=stage_ctx, guard=guard)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_checkpoint(ctx: AgentTurnContext):
        """注入优先；否则装好回退实现（长回答续写检查点）。"""
        injected = ctx.extra.get("persist_continuation_checkpoint")
        if callable(injected):
            return injected

        async def _fallback(checkpoint: dict[str, Any]) -> None:
            from app.services.chat_service import _persist_continuation_checkpoint

            session_factory = ctx.extra.get("persistence_session_factory")
            await _persist_continuation_checkpoint(
                session_factory, ctx.assistant_msg, ctx.run_id, checkpoint
            )

        return _fallback
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && pytest tests/test_run_environment.py -q --tb=short
```
Expected: 3 passed

- [ ] **Step 5: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/run_environment.py backend/tests/test_run_environment.py
git commit -m "feat(agents): RunEnvironment 骨架与 for_turn 构造器"
```

---

### Task 2: `RunEnvironment` 生命周期方法

**Files:**
- Modify: `backend/app/agents/run_environment.py`
- Test: `backend/tests/test_run_environment.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `RunEnvironment`；`ApprovalBridge`（`app.agents.approval_bridge`）；`persist_graph_snapshot`（`app.agents.persistence`）；`db_mutation_scope`（`app.agents.db_mutation`）
- Produces:
  - `attach_graph(graph: AgentGraph) -> AgentLifecycleEmitter`
  - `begin() -> None`
  - `step_started(step_id: str, *, title: str | None = None) -> bool`
  - `step_completed(step_id: str, *, output: str | None = None, output_summary: str | None = None, usage: dict | None = None, usage_charged: bool = False) -> None`
  - `step_failed(step_id: str, *, error: str) -> None`
  - `step_cancelled(step_id: str) -> None`
  - `finish(status: str) -> None`
  - `persist_graph(*, definition: bool) -> None`
  - `aggregate_usage(results: Mapping[str, Any]) -> dict | None`
  - 属性 `emitter: AgentLifecycleEmitter`（`attach_graph` 前访问抛 `RuntimeError`）

- [ ] **Step 1: 写失败的测试**

追加到 `backend/tests/test_run_environment.py`：

```python
import pytest

from app.agents.graph import build_deep_research_graph
from app.agents.runtime.stage_executor import StageResult


async def _env_with_graph(db_session):
    ctx = await _seed_ctx(db_session)
    env = RunEnvironment.for_turn(ctx)
    env.attach_graph(build_deep_research_graph("q"))
    return env


async def test_begin_emits_graph_and_running_status(db_session):
    env = await _env_with_graph(db_session)
    env.begin()
    kinds = _drain(env)
    assert kinds[0] == "agent_graph"
    assert kinds[1] == "run_status"
    assert env.emitter.graph.status == "running"


async def test_attach_graph_builds_approval_bridge(db_session):
    env = await _env_with_graph(db_session)
    assert env.stage_ctx.approval_bridge is not None
    assert env.emitter.graph.run_id == env.run_id


async def test_emitter_before_attach_raises(db_session):
    ctx = await _seed_ctx(db_session)
    env = RunEnvironment.for_turn(ctx)
    with pytest.raises(RuntimeError):
        _ = env.emitter


async def test_step_lifecycle_transitions_are_honest(db_session):
    env = await _env_with_graph(db_session)
    env.begin()
    _drain(env)

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
    _drain(env)
    env.step_started("researcher")
    env.step_failed("researcher", error="boom")

    assert env.emitter.graph.node("researcher").status.value == "failed"
    assert env.emitter.graph.node("analyst").status.value == "cancelled"
    assert env.emitter.graph.node("writer").status.value == "cancelled"


async def test_step_completed_charges_guard_once(db_session):
    env = await _env_with_graph(db_session)
    env.begin()
    _drain(env)
    env.step_started("researcher")
    env.step_completed(
        "researcher", output="x", usage={"total_tokens": 10, "cost_usd": 0.5}
    )
    snapshot = env.guard.snapshot()
    assert snapshot["total_tokens"] == 10

    # usage_charged=True 的表征调用方已实时计费，不得重复累加。
    env.step_started("analyst")
    env.step_completed(
        "analyst", output="y",
        usage={"total_tokens": 7, "cost_usd": 0.2}, usage_charged=True,
    )
    assert env.guard.snapshot()["total_tokens"] == 10


async def test_finish_sets_terminal_status(db_session):
    env = await _env_with_graph(db_session)
    env.begin()
    _drain(env)
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


def _drain(env) -> list[str]:
    """取出队列里当前累积的事件 kind（不阻塞）。"""
    kinds: list[str] = []
    while not env.stage_ctx.queue.empty():
        evt = env.stage_ctx.queue.get_nowait()
        if evt is not None:
            kinds.append(evt.kind)
    return kinds
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && pytest tests/test_run_environment.py -q --tb=short
```
Expected: FAIL — `AttributeError: 'RunEnvironment' object has no attribute 'attach_graph'`

- [ ] **Step 3: 写实现**

在 `backend/app/agents/run_environment.py` 的 `RunEnvironment` 内追加：

```python
    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #
    def attach_graph(self, graph: AgentGraph) -> AgentLifecycleEmitter:
        """建 emitter + 审批桥。审批桥依赖 emitter（等待态由它发），故在此时建。"""
        from app.agents.approval_bridge import ApprovalBridge

        graph.run_id = self.run_id
        emitter = AgentLifecycleEmitter(
            run_id=self.ctx.run_id, graph=graph, stage_ctx=self.stage_ctx
        )
        bridge = ApprovalBridge(
            loop=self.stage_ctx.loop,
            stage_ctx=self.stage_ctx,
            emitter=emitter,
            run_id=self.ctx.run_id,
        )
        self.stage_ctx.approval_bridge = bridge
        self._emitter = emitter
        return emitter

    @property
    def emitter(self) -> AgentLifecycleEmitter:
        if self._emitter is None:
            raise RuntimeError(
                "RunEnvironment.attach_graph() must be called before emitter is used"
            )
        return self._emitter

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def begin(self) -> None:
        """发出 agent_graph + run_status(running)。"""
        self.emitter.emit_graph_initialized()
        self.emitter.emit_run_status("running")

    def step_started(self, step_id: str, *, title: str | None = None) -> bool:
        return self.emitter.emit_agent_started(step_id, task_title=title)

    def step_completed(
        self,
        step_id: str,
        *,
        output: str | None = None,
        output_summary: str | None = None,
        usage: dict | None = None,
        usage_charged: bool = False,
    ) -> None:
        """记完成。usage 的计费只在此处发生一次（usage_charged 表示调用方
        已实时计费，不得重复累加）。"""
        cost: float | None = usage.get("cost_usd") if usage else None
        if usage and self.guard is not None and not usage_charged:
            if cost is None and self.stage_ctx.model_config is not None:
                from app.core.pricing import usage_cost

                cost = usage_cost(
                    getattr(self.stage_ctx.model_config, "model_name", None), usage
                )
            self.guard.add_usage(
                usage, cost_usd=cost, usage_id=f"crewai:stage:{step_id}"
            )
            self.guard.check()
        self.emitter.emit_agent_completed(
            step_id, output_summary=output_summary or None
        )

    def step_failed(self, step_id: str, *, error: str) -> None:
        self.emitter.emit_agent_failed(step_id, error=error)
        self.emitter.cancel_downstream(step_id)

    def step_cancelled(self, step_id: str) -> None:
        self.emitter.emit_agent_cancelled(step_id)

    def finish(self, status: str) -> None:
        self.emitter.emit_run_status(status)

    # ------------------------------------------------------------------ #
    # 持久化与归集
    # ------------------------------------------------------------------ #
    async def persist_graph(self, *, definition: bool) -> None:
        """把图快照写到 AgentRun 行（best-effort）。"""
        from app.agents.db_mutation import db_mutation_scope
        from app.agents.persistence import persist_graph_snapshot

        session_factory = (
            self.ctx.extra.get("persistence_session_factory") or AsyncSessionLocal
        )
        try:
            async with db_mutation_scope(self.ctx.extra.get("persistence_lock")):
                await persist_graph_snapshot(
                    session_factory,
                    run_id=self.ctx.run_id,
                    snapshot=self.emitter.snapshot(),
                    definition=definition,
                )
        except BaseException as exc:
            if isinstance(exc, Exception):
                logger.warning("failed to persist agent graph snapshot", exc_info=True)
                return
            raise

    def aggregate_usage(self, results: Mapping[str, Any]) -> dict | None:
        from app.agents.continuation import aggregate_usage

        rounds: list[dict[str, Any] | None] = []
        charged_prefixes: list[str] = []
        for step_id, result in results.items():
            if result is None:
                continue
            if getattr(result, "usage", None):
                rounds.append(result.usage)
                if getattr(result, "usage_charged", False):
                    charged_prefixes.append(f"model:{step_id}:")
            elif isinstance(getattr(result, "structured", None), dict):
                rounds.append(result.structured.get("usage"))
        rounds.extend(
            usage
            for key, usage in self.stage_ctx.usage_records.items()
            if not any(key.startswith(p) for p in charged_prefixes)
        )
        return aggregate_usage(rounds)
```

在文件顶部补 import：

```python
from collections.abc import Mapping
```

（`persist_graph` 是 async 的，所以 Task 2 的测试里必须写成
`await env.persist_graph(definition=True)`。）


- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && pytest tests/test_run_environment.py -q --tb=short
```
Expected: 12 passed

- [ ] **Step 5: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/run_environment.py backend/tests/test_run_environment.py
git commit -m "feat(agents): RunEnvironment 生命周期、持久化与 usage 归集"
```

---

### Task 3: `CrewAIRuntime` 改为委托 `RunEnvironment`（行为等价重构）

**Files:**
- Modify: `backend/app/agents/runtime/crewai_runtime.py`（`_run_multi_agent` 约 519-560；`_walk_stages` 约 918-960；`_run_one_stage` 约 958-1040；`_persist_graph` 约 1136-1157；drain 循环约 686-726）
- Test: 既有测试套件（本任务是纯重构，**等价性由既有测试证明**）

**Interfaces:**
- Consumes: Task 1/2 的 `RunEnvironment`
- Produces: `CrewAIRuntime` 内部改用 `env.*`；`_run_one_stage` / `_walk_stages` 的 `emitter` 参数换成 `env: RunEnvironment`

- [ ] **Step 1: 先跑一次基线，确认起点是绿的**

```bash
cd backend && pytest tests/test_agent_graph_lifecycle.py tests/test_debate.py tests/test_agent_phase0.py tests/test_agent_phase1.py tests/test_agent_phase3.py tests/test_agent_phase4.py tests/test_streaming_writer.py tests/test_approval_bus.py -q --tb=short
```
Expected: 全绿。**记下通过数**，Step 6 要对比。若起点已有失败，先停下来报告，不要在这个基线上做重构。

- [ ] **Step 2: 替换 `_run_multi_agent` 的环境装配块**

把 `crewai_runtime.py` 中从 `guard = _guard_for_context(ctx)` 开始、到 `stage_ctx.persist_continuation_checkpoint = persist_checkpoint_fallback` 结束的一整段（约 526-571 行，含 provider 装配、streaming 字段、续写检查点回退），整体替换为：

```python
        env = RunEnvironment.for_turn(ctx)
        stage_ctx = env.stage_ctx
        guard = env.guard
```

保留其后紧跟的 `tools = await self._build_tools(ctx, stage_ctx=stage_ctx)` 一行不动。

- [ ] **Step 3: 替换 emitter / 审批桥装配**

把：

```python
        emitter = AgentLifecycleEmitter(run_id=ctx.run_id, graph=graph, stage_ctx=stage_ctx)

        # Wire the cross-thread approval bridge so dangerous tools pause the
        # agent node + run and resume on user approval.
        approval_bridge = ApprovalBridge(
            loop=stage_ctx.loop, stage_ctx=stage_ctx, emitter=emitter, run_id=ctx.run_id,
        )
        stage_ctx.approval_bridge = approval_bridge

        # Persist the static graph_definition once.
        await self._persist_graph(ctx, emitter, definition=True)
```

替换为：

```python
        env.attach_graph(graph)
        emitter = env.emitter
        approval_bridge = stage_ctx.approval_bridge

        # Persist the static graph_definition once.
        await env.persist_graph(definition=True)
```

- [ ] **Step 4: 替换 drain 循环里的持久化与 run_flow 调用**

在 `run_flow` 内把 `_walk_stages(ctx, stages, emitter, executor, stage_ctx, outputs)` 改为 `_walk_stages(ctx, stages, executor, env, outputs)`，并把 `emitter.emit_graph_initialized()` / `emitter.emit_run_status(...)` 改为 `env.begin()` / `env.finish(...)`：

```python
        async def run_flow() -> None:
            nonlocal run_exc
            try:
                env.begin()
                await self._walk_stages(ctx, stages, executor, env, outputs)
                env.finish("completed")
            except asyncio.CancelledError:
                env.finish("cancelled")
                raise
            except Exception as exc:
                run_exc = exc
                logger.exception("multi-agent flow failed: %s", exc)
                env.finish("failed")
            finally:
                stage_ctx.close()
```

drain 循环里两处 `await self._persist_graph(ctx, emitter, definition=False)` 改为 `await env.persist_graph(definition=False)`。drain 循环尾部（`finally` 之后）的第三处同样处理。

- [ ] **Step 5: 改 `_walk_stages` 与 `_run_one_stage` 的签名与调用**

`_walk_stages` 签名去掉 `emitter: AgentLifecycleEmitter`，换成 `env: RunEnvironment`；内部三处 emitter 调用改为 env：

```python
    async def _walk_stages(
        self,
        ctx: AgentTurnContext,
        stages: list[StageSpec],
        executor: StageExecutor,
        env: RunEnvironment,
        outputs: dict[str, StageResult],
    ) -> None:
```

- `await self._respect_controls(ctx, stage_ctx, emitter)` → `await self._respect_controls(ctx, env)`
- `await self._run_one_stage(group[0], emitter, executor, stage_ctx, outputs)` → `await self._run_one_stage(group[0], executor, env, outputs)`
- 并行分支的 `self._run_one_stage(s, emitter, executor, stage_ctx, outputs)` → `self._run_one_stage(s, executor, env, outputs)`

`_run_one_stage` 改写为：

```python
    async def _run_one_stage(
        self,
        spec: StageSpec,
        executor: StageExecutor,
        env: RunEnvironment,
        outputs: dict[str, StageResult],
    ) -> None:
        """Run a single agent stage with real lifecycle events around it."""
        stage_ctx = env.stage_ctx
        guard = env.guard
        # Build the context string from dependency outputs (the handoff).
        context_parts = []
        for dep_id in spec.depends_on:
            dep = outputs.get(dep_id)
            if dep and dep.raw:
                context_parts.append(f"[{dep_id} output]\n{dep.raw}")
        # Phase 2: inject any user instructions appended since the last stage.
        if stage_ctx.pending_instructions:
            context_parts.append(
                "[用户追加指导]\n" + "\n".join(f"- {i}" for i in stage_ctx.pending_instructions)
            )
            stage_ctx.pending_instructions = []
        context_str = "\n\n".join(context_parts) if context_parts else None

        title = (
            spec.task.description[:80] if hasattr(spec.task, "description") else None
        )
        if not env.step_started(spec.agent_id, title=title):
            # Waiting on a join — the emitter already moved it to waiting. Skip
            # execution until predecessors complete (handled by stage ordering,
            # so this branch is a safety net for malformed graphs).
            return

        try:
            if guard is None:
                result = await executor.execute(
                    agent_id=spec.agent_id,
                    agent=spec.agent,
                    task=spec.task,
                    context=context_str,
                    stage_ctx=stage_ctx,
                )
            else:
                guard.check()
                try:
                    async with asyncio.timeout(guard.remaining_seconds):
                        result = await executor.execute(
                            agent_id=spec.agent_id,
                            agent=spec.agent,
                            task=spec.task,
                            context=context_str,
                            stage_ctx=stage_ctx,
                        )
                except TimeoutError as exc:
                    raise BudgetExceeded(
                        f"time budget ({guard.limits.max_runtime_seconds}s) exceeded"
                    ) from exc
            outputs[spec.agent_id] = result
            env.step_completed(
                spec.agent_id,
                output=result.raw,
                output_summary=result.output_summary or None,
                usage=result.usage,
                usage_charged=result.usage_charged,
            )
        except asyncio.CancelledError:
            env.step_cancelled(spec.agent_id)
            raise
        except Exception as exc:
            logger.exception("stage %s failed: %s", spec.agent_id, exc)
            env.step_failed(spec.agent_id, error=str(exc))
            raise
```

- [ ] **Step 6: 改 `_respect_controls` 签名并删除已迁移方法**

把 `_respect_controls(self, ctx, stage_ctx, emitter)` 改为 `_respect_controls(self, ctx, env)`，函数体开头加 `stage_ctx = env.stage_ctx; emitter = env.emitter`，其余不动。

删除 `CrewAIRuntime._persist_graph` 整个方法（已迁到 `RunEnvironment.persist_graph`）。删除文件里不再使用的 import：`AgentLifecycleEmitter`、`ApprovalBridge`、`persist_graph_snapshot`、`db_mutation_scope`（若别处仍用到则保留——用 `ruff check` 判定，它会报 F401）。

- [ ] **Step 7: 运行既有测试确认等价**

```bash
cd backend && pytest tests/test_agent_graph_lifecycle.py tests/test_debate.py tests/test_agent_phase0.py tests/test_agent_phase1.py tests/test_agent_phase3.py tests/test_agent_phase4.py tests/test_streaming_writer.py tests/test_approval_bus.py -q --tb=short
```
Expected: 与 Step 1 **同样的通过数**，零失败。数量不一致说明重构改变了行为，**不要继续**，回到 Step 2 排查。

- [ ] **Step 8: 跑全量后端套件**

```bash
cd backend && ruff check app tests && pytest tests -q --tb=short \
  --deselect tests/test_agent_phase2.py::test_agent_mode_emits_plan_created \
  --deselect tests/test_agent_phase5.py::test_full_native_agent_path \
  --deselect tests/test_durable_controls.py::test_multi_agent_approval_pauses_then_resumes
```
Expected: 全绿。

- [ ] **Step 9: 提交**

```bash
cd /d/Gitee/MyGPT && git add backend/app/agents/runtime/crewai_runtime.py backend/app/agents/run_environment.py
git commit -m "refactor(agents): CrewAIRuntime 委托 RunEnvironment（行为等价）"
```

---

### Task 4: `graph_from_plan`

**Files:**
- Modify: `backend/app/agents/graph.py`（文件末尾追加）
- Test: `backend/tests/test_graph_from_plan.py`

**Interfaces:**
- Consumes: `Plan`/`Step`（`app.agents.workflow.schemas`）；`build_graph_for_profile`（同文件）；`build_deep_research_plan`/`build_parallel_research_plan`/`build_debate_plan`（`app.agents.workflow.planner`）
- Produces: `graph_from_plan(plan: Plan) -> AgentGraph`

**关键设计（spec §5 的强化）**：拓扑取自 plan；**展示文案复用同 profile 的静态 builder**，因为静态 builder 携带的是产品级中文文案（`role="资料检索"`），而 plan 模板是英文技术描述（`role="researcher"`）。若天真地从 `Step` 推展示字段，面板会退化成英文技术串。安全规则：**只有 builder 的节点 id 集合与 plan 的 step id 集合完全相等时才复用**，否则整个回退到 plan 推导（新 profile 走这条）。

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_graph_from_plan.py`：

```python
"""graph_from_plan：plan 是拓扑真相，静态 builder 提供展示层。

核心保证：对既有三个 profile，graph_from_plan(build_*_plan(q)) 必须与
build_*_graph(q) 完全一致 —— 节点 id、依赖、stage 分组、lane、以及
面向用户的中文文案。任何一项不一致都会在子项目 2 开启引擎路由时
变成面板上的可见回归。
"""
from __future__ import annotations

from app.agents.graph import (
    build_debate_graph,
    build_deep_research_graph,
    build_parallel_research_graph,
    graph_from_plan,
)
from app.agents.workflow.planner import build_deep_research_plan, build_parallel_research_plan
from app.agents.workflow.schemas import Plan, Step

_Q = "对比 PostgreSQL 与 MySQL 的适用场景"


def _edge_pairs(graph) -> set[tuple[str, str]]:
    return {(e.source, e.target) for e in graph.edges}


def test_deep_research_topology_matches_static_graph():
    plan = build_deep_research_plan(_Q)
    got = graph_from_plan(plan)
    want = build_deep_research_graph(_Q)

    assert [n.id for n in got.nodes] == [n.id for n in want.nodes]
    assert _edge_pairs(got) == _edge_pairs(want)
    assert [n.stage for n in got.nodes] == [n.stage for n in want.nodes]
    assert [n.lane for n in got.nodes] == [n.lane for n in want.nodes]


def test_deep_research_presentation_matches_static_graph():
    got = graph_from_plan(build_deep_research_plan(_Q))
    want = build_deep_research_graph(_Q)
    for g, w in zip(got.nodes, want.nodes):
        assert g.name == w.name
        assert g.role == w.role
        assert g.task_title == w.task_title
        assert g.task_summary == w.task_summary


def test_parallel_research_topology_and_presentation_match():
    plan = build_parallel_research_plan(_Q)
    got = graph_from_plan(plan)
    want = build_parallel_research_graph(_Q)

    assert [n.id for n in got.nodes] == [n.id for n in want.nodes]
    assert _edge_pairs(got) == _edge_pairs(want)
    assert [n.stage for n in got.nodes] == [n.stage for n in want.nodes]
    assert [n.lane for n in got.nodes] == [n.lane for n in want.nodes]
    for g, w in zip(got.nodes, want.nodes):
        assert (g.name, g.role, g.task_title, g.task_summary) == (
            w.name, w.role, w.task_title, w.task_summary,
        )


def test_debate_stages_carry_dynamic_side_names():
    from app.agents.workflow.planner import build_debate_plan

    plan = build_debate_plan(_Q)
    got = graph_from_plan(plan)
    nodes = {n.id: n for n in got.nodes}
    assert nodes["advocate-a"].stage == 0
    assert nodes["advocate-b"].stage == 0
    assert nodes["judge"].stage == 1
    # advocate-a 先于 advocate-b 声明 -> lane 0 / 1（面板并排渲染依赖此序）。
    assert nodes["advocate-a"].lane == 0
    assert nodes["advocate-b"].lane == 1


def test_unknown_profile_falls_back_to_plan_derived_copy():
    """新 profile（无静态 builder）不得误用 deep_research 的文案。"""
    plan = Plan(
        version=1, goal="g", profile="brand_new",
        steps=[
            Step(id="alpha", role="scout", name="Scout",
                 task_description="The alpha task.", dependencies=[]),
            Step(id="beta", role="editor", name="Editor",
                 task_description="The beta task.", dependencies=["alpha"]),
        ],
    )
    got = graph_from_plan(plan)
    assert [n.id for n in got.nodes] == ["alpha", "beta"]
    assert [n.name for n in got.nodes] == ["Scout", "Editor"]
    assert got.nodes[0].stage == 0 and got.nodes[1].stage == 1
    assert got.nodes[0].task_summary == "The alpha task."
    assert _edge_pairs(got) == {("alpha", "beta")}


def test_unknown_profile_never_borrows_deep_research_copy():
    plan = Plan(
        version=1, goal="g", profile="brand_new",
        steps=[Step(id="solo", name="Solo", task_description="only")],
    )
    nodes = {n.id: n for n in graph_from_plan(plan).nodes}
    assert "solo" in nodes
    assert "researcher" not in nodes
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && pytest tests/test_graph_from_plan.py -q --tb=short
```
Expected: FAIL — `ImportError: cannot import name 'graph_from_plan'`

- [ ] **Step 3: 写实现**

在 `backend/app/agents/graph.py` 末尾追加：

```python
# --------------------------------------------------------------------------- #
# Plan → Graph：引擎路径的拓扑来源
# --------------------------------------------------------------------------- #
_TASK_SUMMARY_MAX = 200


def graph_from_plan(plan: Any) -> AgentGraph:
    """从 plan 构建拓扑，供引擎路径的 :class:`AgentLifecycleEmitter` 使用。

    拓扑（节点 id、依赖边、stage、lane）**完全取自 plan** —— 这是引擎路径
    存在的意义：plan 是唯一真相。

    展示文案（name / role / task_title / task_summary）复用同 profile 的静态
    builder，因为那些 builder 携带的是产品级中文文案，而 plan 模板是英文技术
    描述。只有当 builder 的节点 id 集合与 plan 的 step id 集合**完全相等**时
    才复用；否则整体回退到 plan 推导 —— 这保证新 profile 不会被误配上
    deep_research 的文案。
    """
    step_ids = [s.id for s in plan.steps]

    presentation: dict[str, AgentGraphNode] = {}
    mode = GraphMode.sequential
    try:
        template = build_graph_for_profile(plan.profile, plan.goal)
        if [n.id for n in template.nodes] and {
            n.id for n in template.nodes
        } == set(step_ids):
            presentation = {n.id: n for n in template.nodes}
            mode = template.mode
    except Exception:  # pragma: no cover - 展示层复用失败不得阻断拓扑构建
        presentation = {}

    stages = _stage_depths(plan)
    lanes = _lane_indexes(stages)
    if not presentation:
        mode = GraphMode.parallel if len(set(stages.values())) < len(stages) else mode

    nodes: list[AgentGraphNode] = []
    for step in plan.steps:
        base = presentation.get(step.id)
        if base is not None:
            nodes.append(base.model_copy(update={"status": AgentNodeStatus.pending}))
            continue
        summary = (step.task_description or "")[:_TASK_SUMMARY_MAX]
        nodes.append(
            AgentGraphNode(
                id=step.id,
                name=step.name or step.id,
                role=step.role or "",
                task_title=step.name or step.id,
                task_summary=summary,
                stage=stages.get(step.id, 0),
                lane=lanes.get(step.id, 0),
            )
        )

    edges: list[AgentGraphEdge] = []
    for step in plan.steps:
        for dep in step.dependencies:
            edges.append(
                AgentGraphEdge(
                    id=f"{dep}-{step.id}",
                    source=dep,
                    target=step.id,
                    type=EdgeType.handoff,
                )
            )

    return AgentGraph(
        run_id="",
        runtime="crewai",
        flow_name=plan.profile or "workflow",
        mode=mode,
        status="pending",
        nodes=nodes,
        edges=edges,
    )


def _stage_depths(plan: Any) -> dict[str, int]:
    """stage = 最长依赖链长度（拓扑深度）。plan 已保证无环。"""
    depth: dict[str, int] = {}
    deps = {s.id: list(s.dependencies) for s in plan.steps}
    for step_id in plan.topological_order():
        if deps.get(step_id):
            depth[step_id] = 1 + max(depth.get(d, 0) for d in deps[step_id])
        else:
            depth[step_id] = 0
    return depth


def _lane_indexes(stages: dict[str, int]) -> dict[str, int]:
    """同 stage 内按声明顺序编号 —— 面板靠它并排渲染并行节点。"""
    lanes: dict[str, int] = {}
    counters: dict[int, int] = {}
    for step_id, stage in stages.items():
        lanes[step_id] = counters.get(stage, 0)
        counters[stage] = lanes[step_id] + 1
    return lanes
```

在 `backend/app/agents/graph.py` 顶部补 import（若尚未存在）：

```python
from typing import Any
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && pytest tests/test_graph_from_plan.py -q --tb=short
```
Expected: 6 passed

- [ ] **Step 5: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/graph.py backend/tests/test_graph_from_plan.py
git commit -m "feat(agents): graph_from_plan —— plan 驱动拓扑，静态 builder 提供展示层"
```

---

### Task 5: 事件契约扩展

**Files:**
- Modify: `backend/app/agents/schemas.py`（`ev_agent_status` 约 342-368；文件末尾追加两个构造器）
- Modify: `backend/app/agents/graph.py`（`AgentGraphNode` 约 63-81）
- Modify: `backend/app/agents/lifecycle.py`（`emit_agent_completed` 约 153-184）
- Modify: `backend/app/core/config.py`（Agent 配置块，`AGENT_WORKFLOW_ENGINE` 附近）
- Test: `backend/tests/test_agent_events.py`（新建）

**Interfaces:**
- Produces:
  - `ev_step_output(*, run_id, agent_id, text: str, truncated: bool, chars: int) -> AgentEvent`（kind = `"step_output"`）
  - `ev_step_progress(*, run_id, agent_id, elapsed_s: float, note: str | None = None) -> AgentEvent`（kind = `"step_progress"`）
  - `ev_agent_status(..., usage: dict | None = None, cost_usd: float | None = None)`
  - `AgentGraphNode.usage: dict[str, int] | None`、`AgentGraphNode.cost_usd: float | None`
  - `AgentLifecycleEmitter.emit_agent_completed(agent_id, *, output_summary=None, usage=None, cost_usd=None)`
  - `Settings.AGENT_RICH_STEP_EVENTS: bool = True`、`Settings.AGENT_STEP_PROGRESS_INTERVAL_S: float = 5.0`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_agent_events.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && pytest tests/test_agent_events.py -q --tb=short
```
Expected: FAIL — `ImportError: cannot import name 'ev_step_output'`

- [ ] **Step 3: 实现 —— `schemas.py`**

在 `ev_agent_status` 的签名中，`error` 之前插入两个参数：

```python
    output_summary: str | None = None,
    error: str | None = None,
    usage: dict[str, Any] | None = None,
    cost_usd: float | None = None,
```

并在函数体的 `if error is not None:` 之前插入：

```python
    if usage is not None:
        data["usage"] = usage
    if cost_usd is not None:
        data["cost_usd"] = cost_usd
```

（把新参数放在 `error` **之后**是为了不破坏任何按位置传参的既有调用点。）

在 `schemas.py` 末尾追加：

```python
def ev_step_output(
    *,
    run_id: uuid.UUID | str,
    agent_id: str,
    text: str,
    truncated: bool,
    chars: int,
) -> AgentEvent:
    """一个 stage 的完整产出（展开态）。

    与 ``agent_status.output_summary``（160 字折叠态摘要）分层共存，不是替换。
    ``chars`` 是**截断前**的真实长度，供 UI 显示「已截断」提示。
    """
    return AgentEvent(
        kind="step_output",
        data={
            "run_id": str(run_id),
            "agent_id": agent_id,
            "text": text,
            "truncated": truncated,
            "chars": chars,
        },
    )


def ev_step_progress(
    *,
    run_id: uuid.UUID | str,
    agent_id: str,
    elapsed_s: float,
    note: str | None = None,
) -> AgentEvent:
    """运行中心跳：让长跑 stage 在面板上有真实进度，而不是静止的「运行中」。"""
    data: dict[str, Any] = {
        "run_id": str(run_id),
        "agent_id": agent_id,
        "elapsed_s": elapsed_s,
    }
    if note is not None:
        data["note"] = note
    return AgentEvent(kind="step_progress", data=data)
```

- [ ] **Step 4: 实现 —— `graph.py` 节点字段**

在 `AgentGraphNode` 的 `output_summary` 与 `error` 之间插入：

```python
    output_summary: str | None = None
    # 该 stage 的 token 用量与成本（rich step events 开启时由 emitter 写入）。
    usage: dict[str, int] | None = None
    cost_usd: float | None = None
    error: str | None = None
```

- [ ] **Step 5: 实现 —— `lifecycle.py` 携带 usage**

把 `emit_agent_completed` 的签名与写入改为：

```python
    def emit_agent_completed(
        self,
        agent_id: str,
        *,
        output_summary: str | None = None,
        usage: dict[str, int] | None = None,
        cost_usd: float | None = None,
    ) -> None:
        node = self.graph.node(agent_id)
        if node is None:
            return
        if node.status == AgentNodeStatus.completed:
            return  # idempotent
        node.status = AgentNodeStatus.completed
        node.finished_at = _now_iso()
        start = self._node_starts.pop(agent_id, None)
        if start is not None:
            node.duration_ms = int((self._time.monotonic() - start) * 1000)
        if output_summary:
            node.output_summary = output_summary
        if usage is not None:
            node.usage = {k: v for k, v in usage.items() if isinstance(v, int)}
        if cost_usd is not None:
            node.cost_usd = cost_usd
        self._emit(ev_agent_status(
            run_id=self.run_id, agent_id=agent_id, status=AgentNodeStatus.completed.value,
            finished_at=node.finished_at, duration_ms=node.duration_ms,
            output_summary=output_summary, usage=usage, cost_usd=cost_usd,
        ))
```

其余（边激活、run_status）保持不变。

- [ ] **Step 6: 实现 —— `config.py` 两个设置**

在 `backend/app/core/config.py` 的 `AGENT_WORKFLOW_ENGINE: str = ""` 之后插入：

```python
    # 丰富 step 事件：stage 完成时透出完整产出（step_output）、运行中发心跳
    # （step_progress）、节点携带 tokens 与成本。纯增量事件与字段，老客户端
    # 忽略即可，故默认开；置假即回到旧行为。
    AGENT_RICH_STEP_EVENTS: bool = True
    # step_progress 心跳间隔（秒）。
    AGENT_STEP_PROGRESS_INTERVAL_S: float = 5.0
```

- [ ] **Step 7: 把 usage 从 `step_completed` 接到节点上**

**这是必须的一步**：Task 2 写 `RunEnvironment.step_completed` 时 `emit_agent_completed` 还没有 usage 参数，只传了 `output_summary`；Step 5 扩展了 emitter 的签名，但没有任何调用点把 usage 传下去——不补这一步，节点上的 tokens/成本永远是 `None`。

把 `backend/app/agents/run_environment.py` 的 `step_completed` 末尾：

```python
        self.emitter.emit_agent_completed(
            step_id, output_summary=output_summary or None
        )
```

替换为：

```python
        self.emitter.emit_agent_completed(
            step_id,
            output_summary=output_summary or None,
            usage=usage,
            cost_usd=cost,
        )
```

（`cost` 是同一函数上半部分已经算好的局部变量：未计费路径走向 `usage_cost` 推导的结果，已实时计费路径直接取 `usage["cost_usd"]`，两者都可能为 `None`。）

在 `backend/tests/test_run_environment.py` 追加断言，把它钉住：

```python
async def test_node_carries_usage_and_cost(db_session):
    env = await _env_with_graph(db_session)
    env.begin()
    _drain(env)
    env.step_started("researcher")
    env.step_completed(
        "researcher", output="证据",
        usage={"total_tokens": 42, "cost_usd": 0.03},
    )
    node = env.emitter.graph.node("researcher")
    assert node.usage == {"total_tokens": 42}
    assert node.cost_usd == 0.03
```

- [ ] **Step 8: 运行测试确认通过**

```bash
cd backend && pytest tests/test_agent_events.py -q --tb=short
```
Expected: 8 passed

- [ ] **Step 9: 跑既有相关套件确认未破坏**

```bash
cd backend && pytest tests/test_agent_graph_lifecycle.py tests/test_debate.py tests/test_run_environment.py -q --tb=short
```
Expected: 全绿（`test_run_environment.py` 此时应为 13 passed —— Task 2 的 12 个 + 本任务新增的 `test_node_carries_usage_and_cost`）

- [ ] **Step 10: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/schemas.py backend/app/agents/graph.py backend/app/agents/lifecycle.py backend/app/core/config.py backend/app/agents/run_environment.py backend/tests/test_agent_events.py backend/tests/test_run_environment.py
git commit -m "feat(agents): step_output / step_progress 事件与节点 usage 契约"
```

---

### Task 6: `RunEnvironment` 透出中间产出

**Files:**
- Modify: `backend/app/agents/run_environment.py`
- Test: `backend/tests/test_run_environment.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `RunEnvironment.step_completed`；Task 5 的 `ev_step_output` 与 `Settings.AGENT_RICH_STEP_EVENTS`
- Produces: `RunEnvironment.step_completed` 在富事件开启且产出非空时发 `step_output`；模块常量 `_STEP_OUTPUT_MAX_CHARS = 20_000`

- [ ] **Step 1: 写失败的测试**

追加到 `backend/tests/test_run_environment.py`：

```python
async def test_step_completed_emits_full_output(db_session):
    env = await _env_with_graph(db_session)
    env.begin()
    _drain(env)
    body = "检索到的证据：" + "x" * 500
    env.step_started("researcher")
    env.step_completed("researcher", output=body, output_summary="摘要")

    events = _drain_events(env)
    outs = [e for e in events if e.kind == "step_output"]
    assert len(outs) == 1
    assert outs[0].data["text"] == body
    assert outs[0].data["truncated"] is False
    assert outs[0].data["chars"] == len(body)
    assert outs[0].data["agent_id"] == "researcher"


async def test_step_output_truncates_at_20000(db_session):
    env = await _env_with_graph(db_session)
    env.begin()
    _drain(env)
    body = "y" * 25_000
    env.step_started("researcher")
    env.step_completed("researcher", output=body)

    outs = [e for e in _drain_events(env) if e.kind == "step_output"]
    assert len(outs) == 1
    assert len(outs[0].data["text"]) == 20_000
    assert outs[0].data["truncated"] is True
    assert outs[0].data["chars"] == 25_000


async def test_step_output_skipped_for_empty_output(db_session):
    env = await _env_with_graph(db_session)
    env.begin()
    _drain(env)
    env.step_started("researcher")
    env.step_completed("researcher", output="")
    assert [e for e in _drain_events(env) if e.kind == "step_output"] == []


async def test_step_output_gated_by_flag(db_session, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_RICH_STEP_EVENTS", False, raising=False)
    env = await _env_with_graph(db_session)
    env.begin()
    _drain(env)
    env.step_started("researcher")
    env.step_completed("researcher", output="正文")
    assert [e for e in _drain_events(env) if e.kind == "step_output"] == []


def _drain_events(env) -> list:
    out = []
    while not env.stage_ctx.queue.empty():
        evt = env.stage_ctx.queue.get_nowait()
        if evt is not None:
            out.append(evt)
    return out
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && pytest tests/test_run_environment.py -q --tb=short
```
Expected: FAIL — 断言 `len(outs) == 1` 实际为 0（未发 `step_output`）

- [ ] **Step 3: 写实现**

在 `run_environment.py` 顶部常量区加：

```python
# 单条 stage 产出的透出上限。超过即截断并在事件上标记 truncated，
# ``chars`` 保留截断前的真实长度供 UI 显示提示。
_STEP_OUTPUT_MAX_CHARS = 20_000
```

在 `RunEnvironment` 内新增两个私有方法：

```python
    # ------------------------------------------------------------------ #
    def _rich_events_enabled(self) -> bool:
        from app.core.config import get_settings

        return bool(getattr(get_settings(), "AGENT_RICH_STEP_EVENTS", False))

    def _emit_step_output(self, step_id: str, text: str | None) -> None:
        from app.agents.schemas import ev_step_output

        body = text or ""
        if not body:
            return
        truncated = len(body) > _STEP_OUTPUT_MAX_CHARS
        if truncated:
            body = body[:_STEP_OUTPUT_MAX_CHARS]
        self.stage_ctx.emit(
            ev_step_output(
                run_id=self.run_id,
                agent_id=step_id,
                text=body,
                truncated=truncated,
                chars=len(text or ""),
            )
        )
```

在 `step_completed` 内、`self.emitter.emit_agent_completed(...)` **之前**插入：

```python
        if self._rich_events_enabled():
            self._emit_step_output(step_id, output)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && pytest tests/test_run_environment.py -q --tb=short
```
Expected: 17 passed（Task 5 结束时的 13 个 + 本任务新增 4 个）

- [ ] **Step 5: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/run_environment.py backend/tests/test_run_environment.py
git commit -m "feat(agents): stage 完整产出透出（20k 上限 + 截断标记）"
```

---

### Task 7: 运行中心跳 `step_progress`

**Files:**
- Modify: `backend/app/agents/run_environment.py`
- Test: `backend/tests/test_run_environment.py`（追加）

**Interfaces:**
- Consumes: Task 5 的 `ev_step_progress`、`Settings.AGENT_STEP_PROGRESS_INTERVAL_S`；Task 2 的 `step_started`/`step_completed`/`step_failed`/`step_cancelled`/`finish`
- Produces: `RunEnvironment` 内部 `_progress_tasks: dict[str, asyncio.Task]`；`started_loop_time` 记于 `_step_started_at: dict[str, float]`；心跳 `note` 取自 `emitter.graph.node(step_id).current_tool["name"]`

- [ ] **Step 1: 写失败的测试**

追加到 `backend/tests/test_run_environment.py`：

```python
async def test_progress_heartbeat_fires_while_running(db_session, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "AGENT_STEP_PROGRESS_INTERVAL_S", 0.02, raising=False
    )
    env = await _env_with_graph(db_session)
    env.begin()
    _drain(env)
    env.step_started("researcher")
    await asyncio.sleep(0.09)

    progresses = [e for e in _drain_events(env) if e.kind == "step_progress"]
    assert len(progresses) >= 2
    assert progresses[0].data["agent_id"] == "researcher"
    assert progresses[0].data["elapsed_s"] >= 0


async def test_progress_heartbeat_reports_current_tool(db_session, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "AGENT_STEP_PROGRESS_INTERVAL_S", 0.02, raising=False
    )
    env = await _env_with_graph(db_session)
    env.begin()
    _drain(env)
    env.step_started("researcher")
    env.emitter.set_current_tool(
        "researcher", call_id="c1", name="web_search", status="running"
    )
    await asyncio.sleep(0.05)

    progresses = [e for e in _drain_events(env) if e.kind == "step_progress"]
    assert progresses
    assert progresses[0].data["note"] == "最近工具：web_search"


async def test_progress_stops_after_completion(db_session, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "AGENT_STEP_PROGRESS_INTERVAL_S", 0.02, raising=False
    )
    env = await _env_with_graph(db_session)
    env.begin()
    _drain(env)
    env.step_started("researcher")
    await asyncio.sleep(0.03)
    env.step_completed("researcher", output="done")
    await asyncio.sleep(0.05)

    _drain_events(env)
    await asyncio.sleep(0.06)
    assert [e for e in _drain_events(env) if e.kind == "step_progress"] == []
    assert env._progress_tasks == {}


async def test_finish_cancels_all_heartbeats(db_session, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "AGENT_STEP_PROGRESS_INTERVAL_S", 0.02, raising=False
    )
    env = await _env_with_graph(db_session)
    env.begin()
    _drain(env)
    env.step_started("researcher")
    await asyncio.sleep(0.03)
    env.finish("completed")
    await asyncio.sleep(0.05)
    assert env._progress_tasks == {}


async def test_heartbeat_gated_by_flag(db_session, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "AGENT_STEP_PROGRESS_INTERVAL_S", 0.02, raising=False
    )
    monkeypatch.setattr(get_settings(), "AGENT_RICH_STEP_EVENTS", False, raising=False)
    env = await _env_with_graph(db_session)
    env.begin()
    _drain(env)
    env.step_started("researcher")
    await asyncio.sleep(0.09)
    assert [e for e in _drain_events(env) if e.kind == "step_progress"] == []
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && pytest tests/test_run_environment.py -q --tb=short -k progress or heartbeat
```
Expected: FAIL — `AttributeError: 'RunEnvironment' object has no attribute '_progress_tasks'`

- [ ] **Step 3: 写实现**

在 `RunEnvironment` 的字段区加：

```python
    _emitter: AgentLifecycleEmitter | None = field(default=None, repr=False)
    # 运行中的心跳任务与各自的单调起点。finish() 会清空它们 —— 不允许泄漏。
    _progress_tasks: dict[str, asyncio.Task] = field(default_factory=dict, repr=False)
    _step_started_at: dict[str, float] = field(default_factory=dict, repr=False)
```

把 `step_started` 改为：

```python
    def step_started(self, step_id: str, *, title: str | None = None) -> bool:
        started = self.emitter.emit_agent_started(step_id, task_title=title)
        if started:
            self._start_progress(step_id)
        return started
```

把 `step_completed` 的开头加 `self._stop_progress(step_id)`，`step_failed` / `step_cancelled` 同样各加一行（放在最前）。`finish` 改为：

```python
    def finish(self, status: str) -> None:
        self._stop_all_progress()
        self.emitter.emit_run_status(status)
```

追加私有方法：

```python
    # ------------------------------------------------------------------ #
    # 心跳
    # ------------------------------------------------------------------ #
    def _start_progress(self, step_id: str) -> None:
        if not self._rich_events_enabled():
            return
        self._stop_progress(step_id)
        self._step_started_at[step_id] = self.stage_ctx.loop.time()
        self._progress_tasks[step_id] = self.stage_ctx.loop.create_task(
            self._progress_loop(step_id)
        )

    def _stop_progress(self, step_id: str) -> None:
        task = self._progress_tasks.pop(step_id, None)
        self._step_started_at.pop(step_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _stop_all_progress(self) -> None:
        for step_id in list(self._progress_tasks):
            self._stop_progress(step_id)

    async def _progress_loop(self, step_id: str) -> None:
        from app.agents.schemas import ev_step_progress
        from app.core.config import get_settings

        loop = self.stage_ctx.loop
        interval = float(
            getattr(get_settings(), "AGENT_STEP_PROGRESS_INTERVAL_S", 5.0) or 5.0
        )
        try:
            while True:
                await asyncio.sleep(interval)
                note = self._progress_note(step_id)
                started = self._step_started_at.get(step_id)
                self.stage_ctx.emit(
                    ev_step_progress(
                        run_id=self.run_id,
                        agent_id=step_id,
                        elapsed_s=round(loop.time() - started, 1) if started else 0.0,
                        note=note,
                    )
                )
        except asyncio.CancelledError:
            raise

    def _progress_note(self, step_id: str) -> str | None:
        """note 取自已有状态（emitter 已把当前工具写在节点上），不新增簿记。"""
        node = self.emitter.graph.node(step_id)
        tool = getattr(node, "current_tool", None) if node is not None else None
        if isinstance(tool, dict) and tool.get("name"):
            return f"最近工具：{tool['name']}"
        return None
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && pytest tests/test_run_environment.py -q --tb=short
```
Expected: 22 passed（Task 6 结束时的 17 个 + 本任务新增 5 个）

- [ ] **Step 5: 跑既有套件确认 walker 未被破坏**

```bash
cd backend && pytest tests/test_agent_graph_lifecycle.py tests/test_debate.py tests/test_agent_phase1.py -q --tb=short
```
Expected: 全绿。心跳是新事件，既有测试若断言「事件序列严格等于某列表」会失败——若有失败，检查断言是否应改为「包含」而非「相等」，**不要**为了迁就测试而关掉心跳。

- [ ] **Step 6: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/run_environment.py backend/tests/test_run_environment.py
git commit -m "feat(agents): 运行中心跳 step_progress（含最近工具）"
```

---

### Task 8: `WorkflowEngine` 只读步骤回调

**Files:**
- Modify: `backend/app/agents/workflow/engine.py`（`__init__` 约 61-74；`_run_with_retries` 约 261-312）
- Test: `backend/tests/test_workflow_engine_hooks.py`

**Interfaces:**
- Consumes: `Step`/`StepObservation`（`app.agents.workflow.schemas`）
- Produces: `WorkflowEngine(..., on_step_start=None, on_step_end=None, on_step_error=None)`，三个回调签名 `Callable[[str], Any]`（收 step_id）/ `Callable[[str, str | None, dict | None], Any]` / `Callable[[str, str], Any]`。回调**返回 awaitable 时被 await**；回调抛出的异常被吞掉并记日志。

**设计约束**：回调是**只读观测**，不得改变引擎的调度、重试或终止语义。回调异常绝不影响执行——与引擎既有的 best-effort 持久化策略一致。

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_workflow_engine_hooks.py`：

```python
"""WorkflowEngine 的只读步骤回调。

回调用于把引擎的步骤生命周期接到 RunEnvironment 上（发 agent_status /
step_output / step_progress）。核心保证：**观测不得影响执行**。
"""
from __future__ import annotations

import pytest

from app.agents.workflow.engine import WorkflowEngine
from app.agents.workflow.executor import RecordingExecutor
from app.agents.workflow.planner import build_deep_research_plan
from app.agents.workflow.schemas import Step, Plan, StepError


async def test_callbacks_receive_step_ids_in_order():
    started: list[str] = []
    ended: list[tuple[str, str]] = []

    async def on_start(step_id: str) -> None:
        started.append(step_id)

    async def on_end(step_id: str, output: str | None, usage: dict | None) -> None:
        ended.append((step_id, output or ""))

    engine = WorkflowEngine(
        executor=RecordingExecutor(outputs={"researcher": "R", "analyst": "A", "writer": "W"}),
        on_step_start=on_start,
        on_step_end=on_end,
    )
    result = await engine.run(build_deep_research_plan("q"))

    assert result.status == "completed"
    assert started == ["researcher", "analyst", "writer"]
    assert [sid for sid, _ in ended] == ["researcher", "analyst", "writer"]
    assert ended[0][1] == "R"


async def test_error_callback_fires_on_permanent_failure():
    errors: list[tuple[str, str]] = []

    async def on_error(step_id: str, message: str) -> None:
        errors.append((step_id, message))

    class Boom:
        async def execute(self, step: Step, upstream):
            raise StepError("nope", transient=False)

    engine = WorkflowEngine(executor=Boom(), on_step_error=on_error)
    result = await engine.run(
        Plan(version=1, goal="g", steps=[Step(id="solo", task_description="t")])
    )

    assert result.status == "failed"
    assert errors == [("solo", "nope")]


async def test_sync_callbacks_are_supported():
    seen: list[str] = []

    engine = WorkflowEngine(
        executor=RecordingExecutor(outputs={"solo": "x"}),
        on_step_start=lambda step_id: seen.append(step_id),
    )
    await engine.run(Plan(version=1, goal="g", steps=[Step(id="solo")] ))
    assert seen == ["solo"]


async def test_callback_exceptions_never_break_the_run():
    """观测绝不 veto 执行 —— 与引擎 best-effort 持久化策略一致。"""

    def exploding(step_id: str) -> None:
        raise RuntimeError("observer bug")

    async def exploding_end(step_id: str, output: str | None, usage: dict | None) -> None:
        raise RuntimeError("observer bug")

    engine = WorkflowEngine(
        executor=RecordingExecutor(outputs={"solo": "x"}),
        on_step_start=exploding,
        on_step_end=exploding_end,
    )
    result = await engine.run(Plan(version=1, goal="g", steps=[Step(id="solo")]))
    assert result.status == "completed"
    assert result.observations["solo"].output == "x"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && pytest tests/test_workflow_engine_hooks.py -q --tb=short
```
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'on_step_start'`

- [ ] **Step 3: 写实现**

在 `WorkflowEngine.__init__` 签名中加入三个关键字参数，并存为字段：

```python
    def __init__(
        self,
        *,
        executor: Any,
        verifier: Verifier | None = None,
        run_id: uuid.UUID | str | None = None,
        session_factory: Any = None,
        max_concurrency: int = 8,
        on_step_start: Any = None,
        on_step_end: Any = None,
        on_step_error: Any = None,
    ) -> None:
        self._executor = executor
        self._verifier = verifier
        self._run_id = _opt_uuid(run_id)
        self._session_factory = session_factory
        self._max_concurrency = max(1, int(max_concurrency))
        self._on_step_start = on_step_start
        self._on_step_end = on_step_end
        self._on_step_error = on_step_error
```

在类内追加：

```python
    # ------------------------------------------------------------------ #
    # 只读步骤回调
    #
    # 引擎的步骤生命周期以回调形式外泄，供 RunEnvironment 发
    # agent_status / step_output / step_progress。这是**观测**通道：
    # 回调不得改变调度、重试或终止语义，其异常被吞掉并记日志 ——
    # 与引擎 best-effort 的持久化策略一致（观测绝不 veto 执行）。
    # ------------------------------------------------------------------ #
    async def _call_hook(self, hook: Any, *args: Any) -> None:
        if hook is None:
            return
        try:
            result = hook(*args)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("workflow step hook failed", exc_info=True)
```

在 `_run_with_retries` 的 `while True:` 循环内、`await self._open_attempt(...)` 之前插入：

```python
                await self._call_hook(self._on_step_start, step.id)
```

在 `await self._close_attempt(step.id, attempt_number, obs)` 之后、`observe_counter("workflow.steps", 1, outcome="done")` 之前插入：

```python
                    await self._call_hook(
                        self._on_step_end, step.id, obs.output, obs.usage
                    )
```

在 `_run_with_retries` 的**永久失败**分支（`logger.warning("workflow step %s failed permanently: %s", ...)` 之后、`return None` 之前）插入：

```python
                await self._call_hook(self._on_step_error, step.id, str(exc))
```

在 `engine.py` 顶部补 import：

```python
import inspect
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && pytest tests/test_workflow_engine_hooks.py -q --tb=short
```
Expected: 4 passed

- [ ] **Step 5: 跑既有引擎套件确认未破坏**

```bash
cd backend && pytest tests/test_workflow_engine.py tests/test_workflow_replan.py tests/test_workflow_queue.py -q --tb=short
```
Expected: 全绿

- [ ] **Step 6: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/workflow/engine.py backend/tests/test_workflow_engine_hooks.py
git commit -m "feat(workflow): 引擎步骤只读回调（观测不得 veto 执行）"
```

---

### Task 9: 引擎路径接线与三处缺陷修复

**Files:**
- Modify: `backend/app/agents/orchestrator.py`（`_run_engine_path` 约 359-527；`_build_stage_adapter` 约 528-551）
- Test: `backend/tests/test_engine_routing.py`（扩展）

**Interfaces:**
- Consumes: Task 1/2/4/6/7 的 `RunEnvironment`；Task 4 的 `graph_from_plan`；Task 8 的引擎回调
- Produces: `_run_engine_path` 走 `RunEnvironment`；`_EmittingExecutor` 删除（由 env 统一发事件）

**修复的三处缺陷**（spec §6）：
1. `ev_done` 不带 `usage` → 计费漏账
2. `ev_run_status(current_agent_ids=[])` 恒空 → 面板「并行中」不显示
3. 从不写 `AgentRun.graph_state` → 刷新丢链路

- [ ] **Step 1: 改掉两条即将失效的既有断言**

**先读 `backend/tests/test_engine_routing.py` 全文**，再用下面的内容动手——该文件已有完整的 harness（`_seed_ctx` / `_FakeWorkflowExecutor` / `_drive` / `_patch_flag` / `_spy_crewai`），不要另起一套。

`test_engine_runs_when_flag_on_and_profile_is_deep_research` 里有两条断言会**必然失败**：

```python
    assert "step_started" in kinds, f"missing step_started in {kinds}"
    assert "step_completed" in kinds, f"missing step_completed in {kinds}"
```

原因是 crewai walker **从不发** `step_started` / `step_completed`（只有 `native_runtime.py` 发，那是单 Agent 气泡里的 ResearchSteps 轨迹）。引擎路径旧实现自造这两个事件，是**事件词汇的第二真相源**——正是 spec §10「引擎路径与 walker 发出的事件种类集合相同」要求消除的分歧。

把这两条改掉：

```python
    # 引擎路径与 walker 的事件词汇必须一致：walker 不发 step_started /
    # step_completed（那是 native 单 Agent 路径的轨迹事件），引擎路径也不发。
    assert "agent_status" in kinds, f"missing agent_status in {kinds}"
    assert "run_status" in kinds, f"missing run_status in {kinds}"
    assert "step_started" not in kinds, "引擎路径不得自造 walker 不发的 step 事件"
    assert "step_completed" not in kinds, "引擎路径不得自造 walker 不发的 step 事件"
```

- [ ] **Step 2: 给 fake executor 加上 usage，并写新测试**

`_FakeWorkflowExecutor` 目前返回的 `StepObservation` 不带 usage，导致「引擎路径上报 usage」这条断言即使接线正确也会失败（没有 usage 可上报）。给它加一个可选参数：

```python
    def __init__(
        self,
        *,
        fail_step: str | None = None,
        outputs: dict[str, str] | None = None,
        usage: dict[str, int | float] | None = None,
    ) -> None:
        self.fail_step = fail_step
        self.outputs = outputs or {}
        self.usage = usage
        self.calls: list[str] = []

    async def execute(
        self, step: Step, upstream: dict[str, StepObservation]
    ) -> StepObservation:
        self.calls.append(step.id)
        if step.id == self.fail_step:
            raise RuntimeError(f"engine forced failure at {step.id}")
        out = self.outputs.get(step.id, f"[{step.id}] output")
        return StepObservation(step_id=step.id, output=out, usage=self.usage)
```

追加到文件末尾：

```python
# --------------------------------------------------------------------------- #
# 5. 引擎路径的三处缺陷修复（usage / current_agent_ids / graph_state）
# --------------------------------------------------------------------------- #
async def test_engine_path_reports_usage(db_session, monkeypatch):
    """ev_done 必须带 usage —— 否则 credits 对引擎轮次静默漏账。"""
    _patch_flag(monkeypatch, engine="1", crewai=True)
    ctx = await _seed_ctx(db_session)
    ctx.extra["workflow_executor"] = _FakeWorkflowExecutor(
        outputs={"researcher": "ev", "analyst": "f", "writer": "answer"},
        usage={"total_tokens": 120, "prompt_tokens": 80, "completion_tokens": 40,
               "cost_usd": 0.02},
    )
    _spy_crewai(monkeypatch)

    events = await _drive(ChatOrchestrator(), ctx)
    done = [d for k, d in events if k == "done"]

    assert done, "engine path must emit done"
    assert done[0].get("usage"), "engine path must report usage"
    assert done[0]["usage"]["total_tokens"] == 360  # 三个 step 各 120
    assert ctx.extra["usage"]["total_tokens"] == 360


async def test_engine_path_reports_active_agents(db_session, monkeypatch):
    """run_status 必须列出真实运行中的 agent，否则面板「并行中」不显示。"""
    _patch_flag(monkeypatch, engine="1", crewai=True)
    ctx = await _seed_ctx(db_session)
    ctx.extra["workflow_executor"] = _FakeWorkflowExecutor(
        outputs={"researcher": "ev", "analyst": "f", "writer": "answer"}
    )
    _spy_crewai(monkeypatch)

    events = await _drive(ChatOrchestrator(), ctx)
    statuses = [d for k, d in events if k == "run_status"]

    assert statuses, "engine path must emit run_status"
    # 旧实现恒发 current_agent_ids=[]，面板因此永远显示不出「N 并行中」。
    assert any(d.get("current_agent_ids") for d in statuses), (
        f"no run_status carried active agents: {statuses}"
    )


async def test_engine_path_persists_graph_state(db_session, monkeypatch):
    """引擎路径必须写 graph_state，否则刷新后链路丢失。"""
    from app.models import AgentRun

    _patch_flag(monkeypatch, engine="1", crewai=True)
    ctx = await _seed_ctx(db_session)
    ctx.extra["workflow_executor"] = _FakeWorkflowExecutor(
        outputs={"researcher": "ev", "analyst": "f", "writer": "answer"}
    )
    _spy_crewai(monkeypatch)

    await _drive(ChatOrchestrator(), ctx)

    row = (
        await db_session.execute(select(AgentRun).where(AgentRun.id == ctx.run_id))
    ).scalar_one()
    await db_session.refresh(row)
    assert row.graph_state, "engine path must persist graph_state"
    assert row.graph_definition, "engine path must persist graph_definition once"


async def test_engine_path_topology_comes_from_plan(db_session, monkeypatch):
    """拓扑来自 plan（graph_from_plan），不是硬编码的 build_deep_research_graph。"""
    _patch_flag(monkeypatch, engine="1", crewai=True)
    ctx = await _seed_ctx(db_session)
    ctx.extra["workflow_executor"] = _FakeWorkflowExecutor(
        outputs={"researcher": "ev", "analyst": "f", "writer": "answer"}
    )
    _spy_crewai(monkeypatch)

    events = await _drive(ChatOrchestrator(), ctx)
    graph_evt = [d for k, d in events if k == "agent_graph"][0]
    nodes = graph_evt["graph"]["nodes"]

    assert [n["id"] for n in nodes] == ["researcher", "analyst", "writer"]
    # 展示层复用静态 builder，所以中文产品文案得以保留（不是 plan 里的英文技术串）。
    assert nodes[0]["role"] == "资料检索"
    assert nodes[0]["task_title"] == "检索和整理证据"


async def test_engine_and_walker_emit_the_same_lifecycle_event_kinds(
    db_session, monkeypatch
):
    """引擎路径与 CrewAI walker 的事件种类集合必须一致（spec §10）。"""
    from app.agents.runtime.stage_executor import FakeStageExecutor

    # --- 引擎路径 ---
    _patch_flag(monkeypatch, engine="1", crewai=True)
    ctx_engine = await _seed_ctx(db_session)
    ctx_engine.extra["workflow_executor"] = _FakeWorkflowExecutor(
        outputs={"researcher": "ev", "analyst": "f", "writer": "answer"}
    )
    _spy_crewai(monkeypatch)
    engine_events = await _drive(ChatOrchestrator(), ctx_engine)

    # --- walker 路径 ---
    _patch_flag(monkeypatch, engine="", crewai=True)
    ctx_walker = await _seed_ctx(db_session)
    ctx_walker.extra["stage_executor"] = FakeStageExecutor()
    walker_events = await _drive(ChatOrchestrator(), ctx_walker)

    lifecycle = {"agent_graph", "agent_status", "agent_edge", "run_status", "token", "done"}
    engine_kinds = {k for k, _ in engine_events} & lifecycle
    walker_kinds = {k for k, _ in walker_events} & lifecycle

    assert engine_kinds == walker_kinds, (
        f"engine={sorted(engine_kinds)} walker={sorted(walker_kinds)}"
    )
```

- [ ] **Step 3: 运行测试确认失败**

```bash
cd backend && AGENT_WORKFLOW_ENGINE=1 pytest tests/test_engine_routing.py -q --tb=short
```
Expected: FAIL — `test_engine_path_reports_usage`（`ev_done` 无 usage）、`test_engine_path_reports_active_agents`（`current_agent_ids` 恒空）、`test_engine_path_persists_graph_state`（无 `graph_state`）、`test_engine_path_topology_comes_from_plan`（`role` 是 `"researcher"` 而非 `"资料检索"`）

- [ ] **Step 4: 改写 `_run_engine_path`**

把 `_run_engine_path` 中从 `injected = ctx.extra.get("workflow_executor")` 到 `yield ev_done(message_id=..., finish_reason="stop")` 的整段替换为：

```python
        env = RunEnvironment.for_turn(ctx)
        env.attach_graph(graph_from_plan(plan))
        env.begin()
        # 图定义只落一次，与 walker 路径一致（刷新后可恢复链路）。
        await env.persist_graph(definition=True)

        injected = ctx.extra.get("workflow_executor")
        if injected is not None:
            inner = injected
        else:
            inner = self._build_stage_adapter(ctx, run, env)

        class _EnvExecutor:
            """把引擎的步骤执行接到 RunEnvironment 的共享上下文上。"""

            async def execute(self, step: Any, upstream: dict) -> Any:
                try:
                    obs = await inner.execute(step, upstream)
                except (BudgetExceeded, PromptAdmissionError) as exc:
                    raise StepError(str(exc), transient=False) from exc
                return obs

        engine = WorkflowEngine(
            executor=_EnvExecutor(),
            verifier=RuleBasedVerifier(),
            run_id=run.id,
            session_factory=ctx.extra.get("persistence_session_factory"),
            on_step_start=lambda step_id: env.step_started(step_id),
            on_step_end=lambda step_id, output, usage: env.step_completed(
                step_id,
                output=output,
                output_summary=(output or "")[:160] or None,
                usage=usage,
            ),
            on_step_error=lambda step_id, message: env.step_failed(
                step_id, error=message
            ),
        )

        # 驱动引擎的同时并发排空 env 的事件队列，让面板实时更新 —— 与 walker
        # 路径的 run_flow + drain 同构。若写成 `result = await engine.run(plan)`
        # 再排空，所有过程事件会挤到最后一次性到达，面板在整个多 Agent 轮次
        # 里全程静止。
        #
        # 哨兵（None）由引擎任务在 finally 里投放，保证它排在所有步骤事件之后：
        # stage_ctx.emit 走 call_soon_threadsafe，回调要到下一个 loop tick 才
        # 执行；先 await asyncio.sleep(0) 让已排队的回调落地，再放哨兵。
        result_holder: dict[str, Any] = {}
        engine_exc: list[BaseException] = []

        async def _run_engine() -> None:
            try:
                result_holder["result"] = await engine.run(plan)
            except BaseException as exc:
                engine_exc.append(exc)
            finally:
                await asyncio.sleep(0)
                env.stage_ctx.close()

        engine_task = asyncio.create_task(_run_engine())
        try:
            while True:
                evt = await env.stage_ctx.queue.get()
                if evt is None:
                    break
                yield evt
        finally:
            if not engine_task.done():
                engine_task.cancel()
            try:
                await engine_task
            except (asyncio.CancelledError, Exception):
                pass

        if engine_exc:
            raise engine_exc[0]

        result = result_holder.get("result")
        if result is None or result.status != "completed":
            # 引擎未完成 -> 抛出让上层回退到已验证的 CrewAI 路径。
            raise RuntimeError(
                f"workflow engine did not complete (status="
                f"{getattr(result, 'status', 'missing')}, "
                f"error={getattr(result, 'error', None)})"
            )

        env.finish("completed")
        await asyncio.sleep(0)  # 让 finish 的事件落地（同上）
        for evt in _drain_env_events(env):
            yield evt

        # 终态步骤 = 拓扑序最后一个。模板里它是 writer，但这里**不按名字硬编码**
        # —— 拓扑已由 plan 决定，名字硬编码会在新 profile 上静默取错。
        terminal_step = plan.topological_order()[-1]
        terminal_obs = result.observations.get(terminal_step)
        final_text = (terminal_obs.output if terminal_obs else "") or ""
        ctx.assistant_msg.content = final_text
        if final_text:
            yield ev_token(delta=final_text)

        usage = env.aggregate_usage(result.observations)
        if usage:
            ctx.extra["usage"] = usage
        ctx.extra["finish_reason"] = "stop"
        yield ev_done(
            message_id=ctx.assistant_msg.id,
            finish_reason="stop",
            usage=usage,
            budget=ctx.extra.get("budget"),
        )
```

同时删除 `_EmittingExecutor` 类（引擎现在由 `on_step_*` 回调直连 env，事件经 `stage_ctx.queue` 流出）。原函数里那套 `queue = asyncio.Queue()` + `await queue.put(...)` 的中间队列**整块删除**——事件现在直接进 `stage_ctx.queue`，两套队列并存会让步骤事件走错通道。

同时删除 `_run_engine_path` 里原先直发的 `ev_agent_graph(...)`、`ev_run_status(...)`、`ev_step_started(...)`、`ev_step_completed(...)`、`ev_agent_status(...)`——这些现在全部由 `env.begin()` / `env.step_*` / `env.finish()` 统一发出。`build_deep_research_graph` 的 import 也一并删除（拓扑现由 `graph_from_plan` 提供）。

- [ ] **Step 5: 顶部 import 与 `_drain_env_events` helper**

在 `orchestrator.py` 顶部补：

```python
from app.agents.graph import graph_from_plan
from app.agents.run_environment import RunEnvironment
```

并在文件末尾（`_truthy` 之前）加一个 helper：

```python
def _drain_env_events(env: RunEnvironment) -> list[AgentEvent]:
    """取出 RunEnvironment 队列中累积的事件（非阻塞）。"""
    out: list[AgentEvent] = []
    while not env.stage_ctx.queue.empty():
        evt = env.stage_ctx.queue.get_nowait()
        if evt is not None:
            out.append(evt)
    return out
```

- [ ] **Step 6: `_build_stage_adapter` 接受 env**

把签名改为 `def _build_stage_adapter(self, ctx, run, env)`，并把内部的：

```python
        guard = _guard_for_context(ctx)
        llm = CrewAILLMFactory.from_model_config(ctx.model_config, budget_guard=guard)
        stage_ctx = make_stage_context(str(run.id), budget_guard=guard)
```

替换为：

```python
        guard = env.guard
        llm = CrewAILLMFactory.from_model_config(ctx.model_config, budget_guard=guard)
        stage_ctx = env.stage_ctx
```

删除不再使用的 `_guard_for_context`、`make_stage_context` import（若别处仍用到则保留，由 `ruff check` 判定）。

- [ ] **Step 7: 运行测试确认通过**

```bash
cd backend && AGENT_WORKFLOW_ENGINE=1 pytest tests/test_engine_routing.py -q --tb=short
```
Expected: 全绿（原有 4 个 + 新增 5 个）

- [ ] **Step 8: 跑全量后端套件**

```bash
cd backend && ruff check app tests && pytest tests -q --tb=short \
  --deselect tests/test_agent_phase2.py::test_agent_mode_emits_plan_created \
  --deselect tests/test_agent_phase5.py::test_full_native_agent_path \
  --deselect tests/test_durable_controls.py::test_multi_agent_approval_pauses_then_resumes
```
Expected: 全绿

- [ ] **Step 9: 提交**

```bash
cd /d/Gitee/MyGPT && git add backend/app/agents/orchestrator.py backend/tests/test_engine_routing.py
git commit -m "feat(agents): 引擎路径接入 RunEnvironment，修 usage/current_agent_ids/graph_state"
```

---

### Task 10: 前端类型与 reducer

**Files:**
- Modify: `frontend/src/lib/agent-graph-types.ts`（`AgentGraphNode` 约 33-56）
- Modify: `frontend/src/lib/agent-graph-reducer.ts`（action 联合类型约 25-35；switch 约 62-150）
- Test: `frontend/src/lib/__tests__/agent-graph-reducer.test.ts`（已存在，追加）

**Interfaces:**
- Consumes: 后端 Task 5/6/7 的事件载荷
- Produces:
  - `AgentGraphNode.outputFull?: string`、`outputTruncated?: boolean`、`usage?: {...}`、`costUsd?: number`、`progressNote?: string`
  - `AgentGraphAction` 增加 `{ type: "STEP_OUTPUT"; runId: string; agentId: string; text: string; truncated: boolean; chars: number }` 与 `{ type: "STEP_PROGRESS"; runId: string; agentId: string; elapsedS: number; note?: string }`

- [ ] **Step 1: 写失败的测试**

追加到既有 reducer 测试文件：

```ts
describe("富步骤事件", () => {
  const base = (): AgentGraphState => ({
    runId: "r1",
    runtime: "crewai",
    flowName: "deep_research",
    mode: "sequential",
    status: "running",
    nodes: [
      {
        id: "researcher",
        name: "Researcher",
        role: "资料检索",
        stage: 0,
        status: "running",
      },
    ],
    edges: [],
    activeAgentIds: ["researcher"],
  });

  it("STEP_OUTPUT 写入完整产出与截断标记", () => {
    const next = reducer(base(), {
      type: "STEP_OUTPUT",
      runId: "r1",
      agentId: "researcher",
      text: "证据正文",
      truncated: true,
      chars: 25000,
    });
    const node = next.nodes[0];
    expect(node.outputFull).toBe("证据正文");
    expect(node.outputTruncated).toBe(true);
  });

  it("STEP_OUTPUT 对已完成节点仍然生效（不回归守卫只挡状态）", () => {
    const state = base();
    state.nodes[0].status = "completed";
    const next = reducer(state, {
      type: "STEP_OUTPUT",
      runId: "r1",
      agentId: "researcher",
      text: "迟到的产出",
      truncated: false,
      chars: 5,
    });
    expect(next.nodes[0].status).toBe("completed");
    expect(next.nodes[0].outputFull).toBe("迟到的产出");
  });

  it("STEP_PROGRESS 写入进度提示行", () => {
    const next = reducer(base(), {
      type: "STEP_PROGRESS",
      runId: "r1",
      agentId: "researcher",
      elapsedS: 23,
      note: "最近工具：web_search",
    });
    expect(next.nodes[0].progressNote).toBe("最近工具：web_search");
  });

  it("未知 agent 的富事件被忽略而不是崩溃", () => {
    const next = reducer(base(), {
      type: "STEP_OUTPUT",
      runId: "r1",
      agentId: "nobody",
      text: "x",
      truncated: false,
      chars: 1,
    });
    expect(next.nodes).toHaveLength(1);
    expect(next.nodes[0].outputFull).toBeUndefined();
  });

  it("AGENT_STATUS 携带 usage / costUsd", () => {
    const next = reducer(base(), {
      type: "AGENT_STATUS",
      runId: "r1",
      agentId: "researcher",
      patch: { status: "completed", usage: { total_tokens: 42 }, costUsd: 0.03 },
    });
    expect(next.nodes[0].usage).toEqual({ total_tokens: 42 });
    expect(next.nodes[0].costUsd).toBe(0.03);
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd frontend && npm run test -- agent-graph-reducer
```
Expected: FAIL — TypeScript 报 `STEP_OUTPUT` 不在 `AgentGraphAction` 联合类型中

- [ ] **Step 3: 实现类型**

在 `AgentGraphNode` 的 `outputSummary?: string;` 之后插入：

```ts
  outputSummary?: string;
  /** 该 stage 的完整产出（展开态）。与 outputSummary（160 字折叠态）分层共存。 */
  outputFull?: string;
  /** outputFull 是否被后端按 20k 上限截断。 */
  outputTruncated?: boolean;
  /** 该 stage 的 token 用量。 */
  usage?: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number };
  /** 该 stage 的成本（USD）。 */
  costUsd?: number;
  /** 运行中心跳的最近状态行（如「最近工具：web_search」）。 */
  progressNote?: string;
```

- [ ] **Step 4: 实现 reducer action**

在 `AgentGraphAction` 联合类型中追加：

```ts
  | { type: "STEP_OUTPUT"; runId: string; agentId: string; text: string; truncated: boolean; chars: number }
  | { type: "STEP_PROGRESS"; runId: string; agentId: string; elapsedS: number; note?: string }
```

在 `switch` 中 `case "AGENT_STATUS":` 之前插入：

```ts
    case "STEP_OUTPUT": {
      // 完整产出是「非状态字段」——即使节点已 terminal 也要写入，
      // 否则 stage 完成后到达的产出会被丢弃。
      const nodes = state.nodes.map((n) =>
        n.id === action.agentId
          ? { ...n, outputFull: action.text, outputTruncated: action.truncated }
          : n
      );
      return finalize({ ...state, nodes });
    }

    case "STEP_PROGRESS": {
      const nodes = state.nodes.map((n) =>
        n.id === action.agentId ? { ...n, progressNote: action.note ?? undefined } : n
      );
      return finalize({ ...state, nodes });
    }
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd frontend && npm run test -- agent-graph-reducer
```
Expected: 全绿（含 5 个新用例）

- [ ] **Step 6: typecheck + lint + 提交**

```bash
cd frontend && npm run typecheck && npm run lint
cd /d/Gitee/MyGPT && git add frontend/src/lib/agent-graph-types.ts frontend/src/lib/agent-graph-reducer.ts frontend/src/lib/__tests__/agent-graph-reducer.test.ts
git commit -m "feat(frontend): 节点完整产出 / 进度 / usage 类型与 reducer"
```

---

### Task 11: SSE 桥接新事件

**Files:**
- Modify: `frontend/src/hooks/useChatStream.ts`（agent 事件处理区约 376-400）
- Test: `frontend/src/hooks/__tests__/` **目录尚不存在**，本任务新建它

> **重要**：`useChatStream` 的 SSE 事件解析不宜通过 React hook 挂载来测（需要 mock `EventSource` 与整个聊天流）。本仓库已在 `frontend/src/lib/__tests__/sse-parser.test.ts` 建立了**纯函数测法**。本任务把 `step_output` / `step_progress` 的解析写成同一层的纯函数并直接测——**不要**为了可测而导出 hook 内部函数。

**Interfaces:**
- Consumes: Task 10 的 `STEP_OUTPUT` / `STEP_PROGRESS` action
- Produces:
  - `frontend/src/lib/agent-events.ts` 新增两个纯函数（本任务创建该模块）
  - `parseStepOutput(payload: unknown): { agentId: string; text: string; truncated: boolean; chars: number } | null`
  - `parseStepProgress(payload: unknown): { agentId: string; elapsedS: number; note?: string } | null`

- [ ] **Step 1: 写失败的测试**

新建 `frontend/src/lib/__tests__/agent-events.test.ts`：

```ts
import { describe, expect, it } from "vitest";

import { parseStepOutput, parseStepProgress } from "@/lib/agent-events";

describe("parseStepOutput", () => {
  it("映射 snake_case 载荷", () => {
    expect(
      parseStepOutput({
        run_id: "r1",
        agent_id: "researcher",
        text: "检索到的证据",
        truncated: false,
        chars: 6,
      })
    ).toEqual({
      agentId: "researcher",
      text: "检索到的证据",
      truncated: false,
      chars: 6,
    });
  });

  it("缺 agent_id 时返回 null（不产生无名节点更新）", () => {
    expect(parseStepOutput({ text: "x", truncated: false, chars: 1 })).toBeNull();
  });

  it("缺 text 时返回空串而不是崩溃", () => {
    const parsed = parseStepOutput({ agent_id: "a", truncated: true, chars: 99 });
    expect(parsed?.text).toBe("");
    expect(parsed?.truncated).toBe(true);
    expect(parsed?.chars).toBe(99);
  });

  it("非对象输入返回 null", () => {
    expect(parseStepOutput(null)).toBeNull();
    expect(parseStepOutput("nope")).toBeNull();
  });
});

describe("parseStepProgress", () => {
  it("映射 snake_case 载荷", () => {
    expect(
      parseStepProgress({
        run_id: "r1",
        agent_id: "researcher",
        elapsed_s: 12.5,
        note: "最近工具：web_search",
      })
    ).toEqual({
      agentId: "researcher",
      elapsedS: 12.5,
      note: "最近工具：web_search",
    });
  });

  it("缺 note 时不带 note 字段", () => {
    const parsed = parseStepProgress({ agent_id: "a", elapsed_s: 1 });
    expect(parsed).toEqual({ agentId: "a", elapsedS: 1 });
    expect(parsed && "note" in parsed).toBe(false);
  });

  it("缺 agent_id 时返回 null", () => {
    expect(parseStepProgress({ elapsed_s: 1 })).toBeNull();
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd frontend && npm run test -- agent-events
```
Expected: FAIL — `Failed to resolve import "@/lib/agent-events"`

- [ ] **Step 3: 写实现**

新建 `frontend/src/lib/agent-events.ts`：

```ts
// SSE 事件载荷 → store 字段的纯解析。
//
// 后端的 agent 事件用 snake_case（agent_id / elapsed_s / cost_usd），store 用
// camelCase。把映射收在这里（而不是散在 hook 里）有两个好处：可直接单测，
// 且后端字段改名时只有一处要动。

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : null;
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function num(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

export interface ParsedStepOutput {
  agentId: string;
  text: string;
  truncated: boolean;
  chars: number;
}

/** `step_output`：一个 stage 的完整产出（展开态）。 */
export function parseStepOutput(payload: unknown): ParsedStepOutput | null {
  const raw = asRecord(payload);
  if (!raw) return null;
  const agentId = str(raw.agent_id);
  if (!agentId) return null;
  return {
    agentId,
    text: str(raw.text),
    truncated: raw.truncated === true,
    chars: num(raw.chars),
  };
}

export interface ParsedStepProgress {
  agentId: string;
  elapsedS: number;
  note?: string;
}

/** `step_progress`：运行中心跳。 */
export function parseStepProgress(payload: unknown): ParsedStepProgress | null {
  const raw = asRecord(payload);
  if (!raw) return null;
  const agentId = str(raw.agent_id);
  if (!agentId) return null;
  const parsed: ParsedStepProgress = { agentId, elapsedS: num(raw.elapsed_s) };
  const note = str(raw.note);
  if (note) parsed.note = note;
  return parsed;
}

/** `agent_status` 上的 usage / cost_usd（可选，纯新增字段）。 */
export function parseNodeUsage(payload: unknown): {
  usage?: Record<string, number>;
  costUsd?: number;
} {
  const raw = asRecord(payload);
  if (!raw) return {};
  const out: { usage?: Record<string, number>; costUsd?: number } = {};
  const usage = asRecord(raw.usage);
  if (usage) {
    const mapped: Record<string, number> = {};
    for (const [key, value] of Object.entries(usage)) {
      if (typeof value === "number") mapped[key] = value;
    }
    if (Object.keys(mapped).length > 0) out.usage = mapped;
  }
  if (typeof raw.cost_usd === "number") out.costUsd = raw.cost_usd;
  return out;
}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd frontend && npm run test -- agent-events
```
Expected: 7 passed

- [ ] **Step 5: 接进 `useChatStream.ts`**

**先读 `frontend/src/hooks/useChatStream.ts` 约 360-410 行**，看清该处既有的 `agent_status` 分支写法（事件对象上字段已被转成 camelCase，如 `e.agentId` / `e.runId`），然后在该分支之后加上两个分支：

```ts
        if (e.kind === "step_output") {
          const parsed = parseStepOutput(e);
          if (parsed) {
            useAgentRunStore.getState().dispatch({
              type: "STEP_OUTPUT",
              runId: e.runId,
              agentId: parsed.agentId,
              text: parsed.text,
              truncated: parsed.truncated,
              chars: parsed.chars,
            });
          }
          return;
        }

        if (e.kind === "step_progress") {
          const parsed = parseStepProgress(e);
          if (parsed) {
            useAgentRunStore.getState().dispatch({
              type: "STEP_PROGRESS",
              runId: e.runId,
              agentId: parsed.agentId,
              elapsedS: parsed.elapsedS,
              note: parsed.note,
            });
          }
          return;
        }
```

> 注意：`parseStepOutput(e)` 直接吃事件对象即可 —— 事件对象上既有 camelCase 字段（`agentId`）也有后端原始字段，但本模块只读 snake_case，所以**传入前要确认事件对象保留了 `agent_id` / `elapsed_s` / `cost_usd`**。若该文件已把载荷整体转成 camelCase，则改为把**原始载荷**传进解析函数（读该文件的 SSE 解析层确认，`sse-parser.ts` 是权威）。

在既有 `agent_status` 分支的 patch 构造里并入 usage / cost：

```ts
          ...parseNodeUsage(e),
```

（放在 patch 对象展开处，与既有的 `status` / `output_summary` 等字段并列。）

在文件顶部补 import：

```ts
import { parseNodeUsage, parseStepOutput, parseStepProgress } from "@/lib/agent-events";
```

- [ ] **Step 6: 运行测试 + typecheck 确认通过**

```bash
cd frontend && npm run test -- agent-events && npm run typecheck
```
Expected: 全绿

- [ ] **Step 7: lint + 提交**

```bash
cd frontend && npm run lint
cd /d/Gitee/MyGPT && git add frontend/src/lib/agent-events.ts frontend/src/lib/__tests__/agent-events.test.ts frontend/src/hooks/useChatStream.ts
git commit -m "feat(frontend): SSE 桥接 step_output / step_progress / 节点 usage"
```

---

### Task 12: 面板组件消费

**Files:**
- Modify: `frontend/src/components/agents/agent-node-card.tsx`
- Modify: `frontend/src/components/agents/agent-inline-status.tsx`
- Modify: `frontend/src/components/agents/agent-activity-feed.tsx`
- Test: 三个组件的既有测试文件（用 `ls frontend/src/components/agents/__tests__/` 确认；无则新建 `agent-node-card.test.tsx`）

**Interfaces:**
- Consumes: Task 10 的节点新字段、Task 11 的 store 数据
- Produces: 无需下游消费

- [ ] **Step 1: 写失败的测试**

创建/追加 `frontend/src/components/agents/__tests__/agent-node-card.test.tsx`：

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AgentNodeCard } from "@/components/agents/agent-node-card";
import type { AgentGraphNode } from "@/lib/agent-graph-types";

const node = (patch: Partial<AgentGraphNode> = {}): AgentGraphNode => ({
  id: "researcher",
  name: "Researcher",
  role: "资料检索",
  stage: 0,
  status: "completed",
  durationMs: 12300,
  outputSummary: "摘要",
  ...patch,
});

describe("AgentNodeCard 展开区", () => {
  it("显示完整产出、耗时、tokens 与成本", () => {
    render(
      <AgentNodeCard
        node={node({
          outputFull: "检索到的完整证据正文",
          usage: { total_tokens: 4200 },
          costUsd: 0.031,
        })}
        now={Date.now()}
      />
    );
    // 展开动作按该组件既有的交互方式触发（若默认展开则直接断言）。
    expect(screen.getByText(/检索到的完整证据正文/)).toBeInTheDocument();
    expect(screen.getByText(/12\.3s|12,300ms/)).toBeInTheDocument();
    expect(screen.getByText(/4200/)).toBeInTheDocument();
  });

  it("产出被截断时显示提示", () => {
    render(
      <AgentNodeCard
        node={node({ outputFull: "x", outputTruncated: true })}
        now={Date.now()}
      />
    );
    expect(screen.getByText(/已截断/)).toBeInTheDocument();
  });

  it("无完整产出时回退到摘要", () => {
    render(<AgentNodeCard node={node()} now={Date.now()} />);
    expect(screen.getByText(/摘要/)).toBeInTheDocument();
  });
});
```

追加到 `agent-inline-status.test.tsx`（无则新建）：

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AgentInlineStatus } from "@/components/agents/agent-inline-status";
import { useAgentRunStore } from "@/stores/agent-run-store";

describe("AgentInlineStatus", () => {
  it("运行中显示耗时与最近工具", () => {
    useAgentRunStore.setState({
      active: {
        runId: "r1", runtime: "crewai", flowName: "deep_research",
        mode: "sequential", status: "running",
        nodes: [{
          id: "researcher", name: "Researcher", role: "资料检索",
          stage: 0, status: "running", progressNote: "最近工具：web_search",
        }],
        edges: [], activeAgentIds: ["researcher"],
      },
    });
    render(<AgentInlineStatus />);
    expect(screen.getByText(/web_search/)).toBeInTheDocument();
  });

  it("完成态显示完成而非「并行中」", () => {
    useAgentRunStore.setState({
      active: {
        runId: "r1", runtime: "crewai", flowName: "deep_research",
        mode: "sequential", status: "running",
        nodes: [{
          id: "researcher", name: "Researcher", role: "资料检索",
          stage: 0, status: "completed", durationMs: 12300,
        }],
        edges: [], activeAgentIds: [],
      },
    });
    render(<AgentInlineStatus />);
    expect(screen.getByText(/完成/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd frontend && npm run test -- agent-node-card agent-inline-status
```
Expected: FAIL — 找不到完整产出 / 进度文案

- [ ] **Step 3: 实现 `agent-node-card.tsx`**

在展开区渲染（沿用该组件既有的展开状态与容器结构）：

```tsx
        {node.outputFull ? (
          <div className="space-y-1">
            <div className="max-h-64 overflow-auto rounded bg-muted/40 p-2 text-[11px] leading-relaxed whitespace-pre-wrap">
              {node.outputFull}
            </div>
            {node.outputTruncated && (
              <p className="text-[10px] text-amber-600 dark:text-amber-400">
                产出过长，已截断显示。
              </p>
            )}
          </div>
        ) : (
          node.outputSummary && (
            <p className="text-[11px] text-muted-foreground">{node.outputSummary}</p>
          )
        )}

        <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-muted-foreground">
          {node.durationMs != null && <span>耗时 {(node.durationMs / 1000).toFixed(1)}s</span>}
          {node.usage?.total_tokens != null && <span>tokens {node.usage.total_tokens}</span>}
          {node.costUsd != null && <span>${node.costUsd.toFixed(4)}</span>}
        </div>
```

- [ ] **Step 4: 实现 `agent-inline-status.tsx`**

把 `label` 的计算改为：

```tsx
  const runningNodes = active.activeAgentIds
    .map((id) => active.nodes.find((n) => n.id === id))
    .filter((n): n is AgentGraphNode => !!n);

  const finishedNodes = active.nodes.filter((n) => n.status === "completed");

  let label: string;
  if (multi) {
    if (runningNodes.length > 0) {
      const head = runningNodes[0];
      const note = head.progressNote ? ` · ${head.progressNote}` : "";
      label =
        runningNodes.length > 1
          ? `${runningNodes.map((n) => n.name).join("、")} 并行中${note}`
          : `${head.name} 处理中${note}`;
    } else if (finishedNodes.length > 0) {
      const last = finishedNodes[finishedNodes.length - 1];
      const secs =
        last.durationMs != null ? ` · ${(last.durationMs / 1000).toFixed(1)}s` : "";
      label = `✓ ${last.name} 完成${secs}`;
    } else {
      label = "多 Agent 协作中";
    }
  } else {
    label = "智能助手正在作答";
  }
```

- [ ] **Step 5: 实现 `agent-activity-feed.tsx`**

把 `outputFull` 存在但 `outputSummary` 缺失（或被截断）的节点，在活动流里以完整产出作为条目正文：

```tsx
        const body = node.outputFull ?? node.outputSummary;
        const clipped = node.outputTruncated ? `${body ?? ""}…` : body;
```

> 实施提示：`agent-activity-feed.tsx` 的既有条目结构保持不变，只把正文取值从 `outputSummary` 换成上面的 `clipped` 回退链，避免活动流与节点卡两处文案不一致。

- [ ] **Step 6: 运行测试确认通过**

```bash
cd frontend && npm run test -- agent-node-card agent-inline-status agent-activity-feed
```
Expected: 全绿

- [ ] **Step 7: 全量前端门禁**

```bash
cd frontend && npm run typecheck && npm run lint && npm run test
```
Expected: 全绿

- [ ] **Step 8: 提交**

```bash
cd /d/Gitee/MyGPT && git add frontend/src/components/agents/
git commit -m "feat(frontend): 节点完整产出 / 耗时 / tokens / 成本 / 进度可见"
```

---

## 收尾检查

- [ ] **后端全量**：
  ```bash
  cd backend && ruff check app tests && pytest tests -q --tb=short \
    --deselect tests/test_agent_phase2.py::test_agent_mode_emits_plan_created \
    --deselect tests/test_agent_phase5.py::test_full_native_agent_path \
    --deselect tests/test_durable_controls.py::test_multi_agent_approval_pauses_then_resumes
  ```
- [ ] **前端全量**：`cd frontend && npm run typecheck && npm run lint && npm run test`
- [ ] **迁移检查**：确认 `git status` 中**没有**新增 `backend/migrations/versions/` 文件。若有，推 main 前必须跑 `./scripts/verify_migrations.sh`。
- [ ] **人工验证（关键）**：本地起后端 + 前端，用 `expert` 模式发一个需要联网检索的问题，确认：
  1. 右侧面板可展开已完成 stage，读到**完整产出**（不是 160 字）；
  2. 节点卡显示耗时与 tokens；
  3. 运行中气泡内显示带进度的心跳行；
  4. 刷新页面后链路仍在（`graph_state` 已落库）。
- [ ] **未推送确认**：`git status -sb` 应显示 `ahead N`，未 push。推送与否由用户决定。

## 已知的延后项（不在本计划范围）

1. **引擎重试与 emitter 终态守卫的交互**：`WorkflowEngine` 的 `RetryPolicy.max_retries > 0` 时，第一次失败会让节点进入 `failed` 终态，重试成功后的 `emit_agent_completed` 会把它从 `failed` 翻成 `completed`（该方法的幂等检查只挡 `completed`，`_TERMINAL` 守卫只在 `emit_agent_started` 里）。当前模板的 `RetryPolicy` 为 `max_retries=0` 或仅对 transient 生效，且引擎路径默认关，故不触发。**子项目 2 把 profile 迁到引擎前必须先解决**（给 `emit_agent_completed` 补终态守卫，或让重试复用同一节点的 attempt 而非翻转状态）。
2. **暂停 / 恢复 / 追加指令下沉到引擎**（spec §6.1）：与 profile 迁移一起做，子项目 2。
3. **profile 迁移与 flag 灰度、计划审批门、辩论 UI 入口**：子项目 2。
4. **LLM 规划器 / LLM verifier / 新拓扑**：子项目 3。
