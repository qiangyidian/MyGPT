# 多 Agent 引擎接管 + 规划/验收智能化 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修掉已实证的重试×终态守卫缺陷；把运行控制下沉到引擎；三个 profile 按名单迁移到引擎；辩论获得 UI 入口、计划门改为「计划先行不阻塞」；新增 LLM 规划器、LLM verifier 与两个协作拓扑。

**Architecture:** 沿用子项目 1 建立的 `RunEnvironment` 作为两条 walker 的唯一执行环境入口。本计划在其中补齐**控制消费**（暂停/恢复/取消/指令）与**重试状态表达**；引擎侧新增 `before_step`（可中断）与 `on_step_retry`（只读）两个钩子。LLM 规划器/verifier 都是**带确定性回退**的包装层——模型失败或产出非法一律回退模板/规则版，绝不让运行中断。发布采用名单式灰度，所有新 flag 默认值为安全值。

**Tech Stack:** Python 3.13 / FastAPI / pydantic v2 / asyncio / CrewAI（懒加载）/ pytest；前端 Next.js + TypeScript + zustand。

**Spec:** `docs/superpowers/specs/2026-09-18-agent-engine-takeover-design.md`

## Global Constraints

- **语言**：面向用户文案用中文；注释/文档允许全角标点（`pyproject.toml` 已关掉 `RUF001/RUF002/RUF003`，那不是笔误）。
- **后端门禁**：`ruff check app tests` 必须全绿。仓库**只跑 `ruff check`，不跑 `ruff format`**（有历史格式债，全量 reformat 会冲垮 diff）。ruff 在 CI 钉死为 `0.15.17`。
- **测试命令**（在 `backend/` 下，用 venv 里的 python）：
  ```
  .venv/Scripts/python.exe -m pytest tests -q --tb=short \
    --deselect tests/test_agent_phase2.py::test_agent_mode_emits_plan_created \
    --deselect tests/test_agent_phase5.py::test_full_native_agent_path \
    --deselect tests/test_durable_controls.py::test_multi_agent_approval_pauses_then_resumes
  ```
  上面三个 deselect：前两个是打真实模型端点的网络依赖用例；第三个是已知偶发死锁（会级联一批 `OperationalError`）。
- **前端门禁**（在 `frontend/` 下）：`npm run typecheck && npm run lint && npm run test`
- **零迁移**：本计划不新增数据库表、不新增 `backend/migrations/versions/` 文件。
- **远端名是 `MyGPT`**，不是 `origin`。当前分支 `main`，推 `main` 会触发生产自动部署。
- **测试基线**：本计划开始前后端全量为 **1226 passed**，前端 **251 passed**。每个任务结束都不得低于该基线。
- **`BudgetGuard` 并发约束（不可违反）**：`BudgetGuard` 内部用 `threading.RLock`（`budget_policy.py:124`）保护临界区，因为工具在 worker 线程里调用它。**临界区内不得出现 `await`**——任何「先 `guard.check()` 再 `guard.add_usage()`」的序列必须保持同步，中间不能 yield。
- **默认值**（全部为安全值，合入 main 不改变生产行为）：
  | 配置项 | 默认值 |
  |---|---|
  | `AGENT_WORKFLOW_ENGINE` | `""`（关） |
  | `AGENT_WORKFLOW_ENGINE_PROFILES` | `""`（空名单） |
  | `PLAN_REQUIRE_CONFIRMATION` | `True`（门开着，但默认不阻塞） |
  | `PLAN_CONFIRM_TIMEOUT_S` | `90` |
  | `AGENT_LLM_PLANNER` | `False` |
  | `AGENT_LLM_VERIFIER` | `False` |
  | `AGENT_LLM_PLANNER_MAX_STEPS` | `8` |
  | `AGENT_LLM_PLANNER_TIMEOUT_S` | `8` |
  | `AGENT_LLM_VERIFIER_TIMEOUT_S` | `10` |
- **不做**（spec §2 非目标）：不迁移 native 单 Agent 路径、不做非 writer 的逐 token 流式、不新增数据库表。

---

## 实施顺序说明

任务分三大部分，**必须按顺序执行**：

- **Task 1-4（地基）**：修重试缺陷、控制下沉、引擎钩子、配置项。这是迁移的前提——不修就迁移等于把错误状态推给用户。
- **Task 5-9（接管与交互）**：profile 名单迁移、辩论入口、计划门、前端。
- **Task 10-18（能力）**：LLM 规划器、LLM verifier、两个新拓扑、自动路由、可观测性。

---

## Task 1: 修复重试 × 终态守卫缺陷

**背景（已实证）**：`emit_agent_completed` 的幂等检查只挡 `completed`（`lifecycle.py:164`），不挡 `failed`。而 `_TRANSIENT.max_retries=1`（`planner.py:39`）是活的，所以引擎重试成功会把节点从 `failed` 翻成 `completed`，同时留下三个矛盾状态：`error` 残留、`duration_ms` 归零、状态已翻转。已用探针复现。

**Files:**
- Modify: `backend/app/agents/lifecycle.py`
- Test: `backend/tests/test_agent_lifecycle_retry.py`（新建）

**Interfaces:**
- Consumes: `AgentLifecycleEmitter`（`app.agents.lifecycle`）、`AgentGraph`/`AgentNodeStatus`（`app.agents.graph`）、`StageContext`/`make_stage_context`（`app.agents.stage_context`）
- Produces:
  - `AgentLifecycleEmitter.emit_agent_retrying(agent_id: str, *, attempt: int, error: str) -> None`
  - `AgentGraphNode.retrying: dict[str, Any] | None`（新字段，形如 `{"attempt": 2, "error": "..."}`）
  - `emit_agent_completed` 语义变更：`cancelled` 是唯一被拒绝覆盖的终态；成功时清除 `error`
  - `emit_agent_failed` 语义变更：**不再** pop `_node_starts`（保留起始时间戳）

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_agent_lifecycle_retry.py`：

```python
"""重试 × 终态守卫：引擎的 transient 重试不得留下矛盾状态。

背景：WorkflowEngine 的 RetryPolicy 允许 transient 失败后重试。旧实现里
attempt1 失败会把节点置为终态 failed，attempt2 成功又把 status 翻回
completed，但 error 没清、duration_ms 归零 —— 用户看到「已完成、带错误、
耗时 0ms」的节点。
"""
from __future__ import annotations

import uuid

from app.agents.graph import AgentNodeStatus, build_deep_research_graph
from app.agents.lifecycle import AgentLifecycleEmitter
from app.agents.stage_context import make_stage_context


def _emitter():
    stage_ctx = make_stage_context(str(uuid.uuid4()))
    graph = build_deep_research_graph("q")
    emitter = AgentLifecycleEmitter(
        run_id=uuid.UUID(stage_ctx.run_id), graph=graph, stage_ctx=stage_ctx
    )
    emitter.emit_graph_initialized()
    return emitter, graph


def _drain(stage_ctx) -> list[str]:
    kinds: list[str] = []
    while not stage_ctx.queue.empty():
        evt = stage_ctx.queue.get_nowait()
        if evt is not None:
            kinds.append(evt.kind)
    return kinds


def test_success_after_failure_clears_error_and_keeps_duration():
    emitter, graph = _emitter()
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


def test_emit_agent_retrying_flips_failed_back_to_running():
    emitter, graph = _emitter()
    emitter.emit_agent_started("researcher")
    emitter.emit_agent_failed("researcher", error="timeout")

    emitter.emit_agent_retrying("researcher", attempt=2, error="timeout")

    node = graph.node("researcher")
    assert node.status == AgentNodeStatus.running
    assert node.retrying == {"attempt": 2, "error": "timeout"}


def test_retrying_emits_agent_status_event():
    emitter, stage_ctx = _emitter()
    emitter.emit_agent_started("researcher")
    emitter.emit_agent_failed("researcher", error="timeout")
    _drain(stage_ctx)

    emitter.emit_agent_retrying("researcher", attempt=2, error="timeout")

    events = []
    while not stage_ctx.queue.empty():
        evt = stage_ctx.queue.get_nowait()
        if evt is not None:
            events.append(evt)
    status_events = [e for e in events if e.kind == "agent_status"]
    assert status_events, "retrying 必须发 agent_status"
    assert status_events[0].data["status"] == "running"
    assert status_events[0].data["retrying"] == {"attempt": 2, "error": "timeout"}


def test_completed_clears_retrying_marker():
    emitter, graph = _emitter()
    emitter.emit_agent_started("researcher")
    emitter.emit_agent_failed("researcher", error="timeout")
    emitter.emit_agent_retrying("researcher", attempt=2, error="timeout")

    emitter.emit_agent_completed("researcher", output_summary="ok")

    assert graph.node("researcher").retrying is None


def test_cancelled_is_not_overwritten_by_completed():
    emitter, graph = _emitter()
    emitter.emit_agent_started("researcher")
    emitter.emit_agent_cancelled("researcher")

    emitter.emit_agent_completed("researcher", output_summary="迟到")

    # 用户主动取消是真正的终态，不得被翻回 completed。
    assert graph.node("researcher").status == AgentNodeStatus.cancelled


def test_retrying_ignored_for_cancelled_node():
    emitter, graph = _emitter()
    emitter.emit_agent_started("researcher")
    emitter.emit_agent_cancelled("researcher")

    emitter.emit_agent_retrying("researcher", attempt=2, error="x")

    assert graph.node("researcher").status == AgentNodeStatus.cancelled
    assert graph.node("researcher").retrying is None
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_agent_lifecycle_retry.py -q --tb=short
```
Expected: FAIL — `AttributeError: 'AgentLifecycleEmitter' object has no attribute 'emit_agent_retrying'`

- [ ] **Step 3: 在 `graph.py` 给节点加 `retrying` 字段**

在 `AgentGraphNode` 的 `cost_usd` 之后、`error` 之前插入：

```python
    cost_usd: float | None = None
    # 正在重试（引擎的 transient 重试）：{"attempt": N, "error": "..."}。
    # 仅 informational —— 面板据此显示「第 N 次尝试」。
    retrying: dict[str, Any] | None = None
    error: str | None = None
```

- [ ] **Step 4: 修改 `lifecycle.py` 的 `emit_agent_completed`**

把整个方法替换为：

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
        # cancelled 是用户主动取消的真正终态，不得被翻回 completed。
        # failed 允许覆盖 —— 重试成功本就该覆盖失败（见 emit_agent_retrying）。
        if node.status == AgentNodeStatus.cancelled:
            logger.info(
                "agent_completed: drop (cancelled) node=%s", agent_id
            )
            return
        node.status = AgentNodeStatus.completed
        node.finished_at = _now_iso()
        start = self._node_starts.pop(agent_id, None)
        if start is not None:
            node.duration_ms = int((self._time.monotonic() - start) * 1000)
        if output_summary:
            node.output_summary = output_summary
        # 重试成功后，上一轮的失败信息与重试标记都必须消失。
        node.error = None
        node.retrying = None
        if usage is not None:
            node.usage = {k: v for k, v in usage.items() if isinstance(v, int)}
        if cost_usd is not None:
            node.cost_usd = cost_usd
        self._emit(ev_agent_status(
            run_id=self.run_id, agent_id=agent_id, status=AgentNodeStatus.completed.value,
            finished_at=node.finished_at, duration_ms=node.duration_ms,
            output_summary=output_summary, usage=usage, cost_usd=cost_usd,
        ))
        # Activate outbound handoff edges (evidence/result handed off).
        for e in self.graph.edges:
            if e.source == agent_id and e.status == AgentEdgeStatus.pending:
                self._set_edge(e, AgentEdgeStatus.active)
                # Immediately complete the handoff edge (the data is available
                # now; the downstream node will start when ALL its inbound edges
                # are completed).
                self._set_edge(e, AgentEdgeStatus.completed)
        self._emit(ev_run_status(
            run_id=self.run_id, status=self.graph.status or "running",
            current_agent_ids=self.graph.recompute_active(),
        ))
```

- [ ] **Step 5: 修改 `emit_agent_failed`（不再 pop 起始时间戳）**

把 `emit_agent_failed` 里这段：

```python
        start = self._node_starts.pop(agent_id, None)
        if start is not None:
            node.duration_ms = int((self._time.monotonic() - start) * 1000)
```

替换为：

```python
        # 注意：这里**不** pop _node_starts —— 引擎可能对 transient 失败重试，
        # 保留起始时间戳使重试成功后的 duration_ms 是「从首次开始」的累计耗时
        # （更接近用户感知）。真正的终态结算由 emit_agent_completed /
        # emit_agent_cancelled 负责。
        start = self._node_starts.get(agent_id)
        if start is not None:
            node.duration_ms = int((self._time.monotonic() - start) * 1000)
```

- [ ] **Step 6: 新增 `emit_agent_retrying`**

在 `emit_agent_failed` 之后、`emit_agent_cancelled` 之前插入：

```python
    def emit_agent_retrying(
        self, agent_id: str, *, attempt: int, error: str
    ) -> None:
        """标记一个节点正在重试（引擎的 transient 重试）。

        把节点从 failed 翻回 running，让面板显示「第 N 次尝试」而不是节点
        忽然诈尸。cancelled 节点不接受重试（用户已取消）。
        """
        node = self.graph.node(agent_id)
        if node is None:
            return
        if node.status == AgentNodeStatus.cancelled:
            return
        node.status = AgentNodeStatus.running
        node.retrying = {"attempt": attempt, "error": error}
        node.error = None
        self._emit(ev_agent_status(
            run_id=self.run_id, agent_id=agent_id,
            status=AgentNodeStatus.running.value,
            retrying={"attempt": attempt, "error": error},
        ))
        self._emit(ev_run_status(
            run_id=self.run_id, status=self.graph.status or "running",
            current_agent_ids=self.graph.recompute_active(),
        ))
```

- [ ] **Step 7: 给 `ev_agent_status` 加 `retrying` 参数**

在 `schemas.py` 的 `ev_agent_status` 签名中，`cost_usd` 之后插入参数：

```python
    cost_usd: float | None = None,
    retrying: dict[str, Any] | None = None,
) -> AgentEvent:
```

并在函数体的 `if cost_usd is not None:` 之后插入：

```python
    if retrying is not None:
        data["retrying"] = retrying
```

- [ ] **Step 8: 运行测试确认通过**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_agent_lifecycle_retry.py -q --tb=short
```
Expected: 6 passed

- [ ] **Step 9: 跑既有生命周期测试确认无回归**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_agent_graph_lifecycle.py tests/test_debate.py tests/test_agent_events.py -q --tb=short
```
Expected: 全绿

- [ ] **Step 10: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/lifecycle.py backend/app/agents/graph.py backend/app/agents/schemas.py backend/tests/test_agent_lifecycle_retry.py
git commit -m "fix(agents): 重试成功后清除失败残留，cancelled 不再被 completed 覆盖"
```

---

## Task 2: 引擎接入重试回调

**Files:**
- Modify: `backend/app/agents/workflow/engine.py`
- Modify: `backend/app/agents/run_environment.py`
- Test: `backend/tests/test_workflow_engine_hooks.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `emit_agent_retrying`
- Produces:
  - `WorkflowEngine(..., on_step_retry: Callable[[str, int, str], Any] | None = None)`
  - `RunEnvironment.step_retrying(step_id: str, *, attempt: int, error: str) -> None`

- [ ] **Step 1: 写失败的测试**

追加到 `backend/tests/test_workflow_engine_hooks.py`：

```python
async def test_retry_callback_fires_on_transient_failure():
    """transient 失败重试时，on_step_retry 必须被调用（观测通道）。"""
    from app.agents.workflow.schemas import RetryPolicy

    retries: list[tuple[str, int, str]] = []
    calls: list[str] = []

    async def on_retry(step_id: str, attempt: int, error: str) -> None:
        retries.append((step_id, attempt, error))

    class FlakyOnce:
        """首次抛 transient，之后成功。"""

        def __init__(self) -> None:
            self.n = 0

        async def execute(self, step, upstream):
            self.n += 1
            calls.append(step.id)
            if self.n == 1:
                raise StepError("connection reset", transient=True)
            from app.agents.workflow.schemas import StepObservation

            return StepObservation(step_id=step.id, output="ok")

    plan = Plan(
        version=1, goal="g",
        steps=[Step(id="solo", retry_policy=RetryPolicy(max_retries=1))],
    )
    engine = WorkflowEngine(executor=FlakyOnce(), on_step_retry=on_retry)
    result = await engine.run(plan)

    assert result.status == "completed"
    assert calls == ["solo", "solo"], "应当重试一次"
    assert len(retries) == 1
    assert retries[0][0] == "solo"
    assert retries[0][1] == 2  # 即将进行的第 2 次尝试
    assert "connection reset" in retries[0][2]


async def test_retry_callback_not_fired_on_permanent_failure():
    retries: list[tuple[str, int, str]] = []

    class AlwaysFails:
        async def execute(self, step, upstream):
            raise StepError("nope", transient=False)

    engine = WorkflowEngine(
        executor=AlwaysFails(),
        on_step_retry=lambda sid, att, err: retries.append((sid, att, err)),
    )
    result = await engine.run(
        Plan(version=1, goal="g", steps=[Step(id="solo")])
    )

    assert result.status == "failed"
    assert retries == [], "永久失败不应触发重试回调"


async def test_retry_callback_exception_never_breaks_the_run():
    class FlakyOnce:
        def __init__(self) -> None:
            self.n = 0

        async def execute(self, step, upstream):
            self.n += 1
            if self.n == 1:
                raise StepError("timeout", transient=True)
            from app.agents.workflow.schemas import StepObservation

            return StepObservation(step_id=step.id, output="ok")

    def exploding(sid, att, err) -> None:
        raise RuntimeError("observer bug")

    engine = WorkflowEngine(
        executor=FlakyOnce(), on_step_retry=exploding
    )
    result = await engine.run(
        Plan(version=1, goal="g",
             steps=[Step(id="solo", retry_policy=RetryPolicy(max_retries=1))])
    )
    assert result.status == "completed"
```

该文件顶部需补 import（若无）：

```python
from app.agents.workflow.schemas import RetryPolicy
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_workflow_engine_hooks.py -q --tb=short
```
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'on_step_retry'`

- [ ] **Step 3: 在 `WorkflowEngine.__init__` 加参数**

在 `on_step_error: Any = None,` 之后加：

```python
        on_step_retry: Any = None,
```
并在方法体 `self._on_step_error = on_step_error` 之后加：

```python
        self._on_step_retry = on_step_retry
```

- [ ] **Step 4: 在 transient 分支调用回调**

在 `_run_with_retries` 里找到：

```python
                if transient:
                    observe_counter("workflow.steps", 1, outcome="retry")
                    continue
```

替换为：

```python
                if transient:
                    observe_counter("workflow.steps", 1, outcome="retry")
                    await self._call_hook(
                        self._on_step_retry, step.id, attempt + 1, str(exc)
                    )
                    continue
```

（`attempt` 是刚失败的这次；`attempt + 1` 是即将进行的下一次。）

- [ ] **Step 5: 在 `RunEnvironment` 加 `step_retrying`**

在 `step_failed` 之后插入：

```python
    def step_retrying(self, step_id: str, *, attempt: int, error: str) -> None:
        """标记节点正在重试（引擎 transient 重试的观测通道）。"""
        self.emitter.emit_agent_retrying(step_id, attempt=attempt, error=error)
```

- [ ] **Step 6: 运行测试确认通过**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_workflow_engine_hooks.py -q --tb=short
```
Expected: 7 passed

- [ ] **Step 7: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/workflow/engine.py backend/app/agents/run_environment.py backend/tests/test_workflow_engine_hooks.py
git commit -m "feat(workflow): 引擎 transient 重试回调 on_step_retry"
```

---

## Task 3: 运行控制下沉到 `RunEnvironment`

**背景**：`run_control` 已在 `ctx.extra`（`orchestrator.py:128` 注册），但引擎路径零消费——用户按暂停无反应。`_respect_controls` 与 `_drain_durable_commands` 目前是 `CrewAIRuntime` 的私有方法，要搬到 `RunEnvironment` 供两条 walker 共用。

**Files:**
- Modify: `backend/app/agents/run_environment.py`
- Modify: `backend/app/agents/runtime/crewai_runtime.py`（改为委托）
- Test: `backend/tests/test_run_environment_controls.py`（新建）

**Interfaces:**
- Consumes: `RunControl`/`get_or_create`（`app.agents.run_controls`）、`CommandStore`（`app.agents.workflow.repository`）、`BudgetExceeded`（`app.agents.policies`）
- Produces:
  - `RunEnvironment.respect_controls() -> None`（async；可抛 `CancelledError`，可阻塞于暂停）
  - `RunEnvironment.drain_durable_commands(ctl) -> None`（async）
  - `CrewAIRuntime._respect_controls` / `_drain_durable_commands` 删除（改为调 env）

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_run_environment_controls.py`：

```python
"""运行控制下沉：暂停 / 恢复 / 取消 / 追加指令 + 持久命令 drain。

引擎路径之前完全不消费 run_control —— 用户按暂停没有任何反应。这两个方法
搬到 RunEnvironment 后，两条 walker 共享同一套控制语义。
"""
from __future__ import annotations

import asyncio

import pytest

from app.agents.run_controls import get_or_create


async def _env(db_session):
    from tests.test_run_environment import _env_with_graph

    return await _env_with_graph(db_session)


async def test_cancel_raises_cancelled_error(db_session):
    env = await _env(db_session)
    ctl = get_or_create(env.run_id)
    ctl.cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await env.respect_controls()


async def test_instructions_are_drained_into_stage_ctx(db_session):
    env = await _env(db_session)
    ctl = get_or_create(env.run_id)
    ctl.add_instruction("聚焦 X")

    await env.respect_controls()

    assert env.stage_ctx.pending_instructions == ["聚焦 X"]
    assert ctl.drain_instructions() == [], "指令必须被一次性取走"


async def test_instruction_emits_event(db_session):
    env = await _env(db_session)
    await asyncio.sleep(0)
    while not env.stage_ctx.queue.empty():
        env.stage_ctx.queue.get_nowait()
    ctl = get_or_create(env.run_id)
    ctl.add_instruction("跳过 Y")

    await env.respect_controls()
    await asyncio.sleep(0)

    kinds = []
    while not env.stage_ctx.queue.empty():
        evt = env.stage_ctx.queue.get_nowait()
        if evt is not None:
            kinds.append(evt.kind)
    assert "run_instruction_received" in kinds


async def test_pause_blocks_until_resumed(db_session):
    env = await _env(db_session)
    ctl = get_or_create(env.run_id)
    ctl.pause()

    task = asyncio.create_task(env.respect_controls())
    await asyncio.sleep(0.05)
    assert not task.done(), "暂停中 respect_controls 不应返回"

    ctl.resume()
    await asyncio.wait_for(task, timeout=1.0)


async def test_pause_then_cancel_unblocks(db_session):
    env = await _env(db_session)
    ctl = get_or_create(env.run_id)
    ctl.pause()

    task = asyncio.create_task(env.respect_controls())
    await asyncio.sleep(0.05)
    ctl.cancel.set()
    await asyncio.wait_for(task, timeout=1.0)


async def test_no_run_control_is_a_noop(db_session):
    env = await _env(db_session)
    env.ctx.extra.pop("run_control", None)
    get_or_create(env.run_id)  # 注册空控制，不应阻塞
    await asyncio.wait_for(env.respect_controls(), timeout=1.0)
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_run_environment_controls.py -q --tb=short
```
Expected: FAIL — `AttributeError: 'RunEnvironment' object has no attribute 'respect_controls'`

- [ ] **Step 3: 在 `RunEnvironment` 加两个方法**

在 `finish` 之后插入：

```python
    # ------------------------------------------------------------------ #
    # 运行控制（两条 walker 共享）
    # ------------------------------------------------------------------ #
    async def respect_controls(self) -> None:
        """消费用户控制：取消 / 暂停 / 追加指令 + 持久命令 drain。

        语义与 walker 的 _respect_controls 完全一致 —— 两条路径必须在
        「按暂停真的会停」这件事上表现相同，否则用户会认为按钮坏了。
        """
        from app.agents.run_controls import get_or_create as _get_or_create

        ctl = self.ctx.extra.get("run_control") or _get_or_create(self.run_id)
        if ctl is None:
            return
        guard = self.guard
        if guard is not None:
            guard.check()
        # 用户主动取消：抛 CancelledError 让调用方终止。
        if ctl.cancel.is_set():
            raise asyncio.CancelledError()
        # 持久命令 drain（exactly-once claim → apply → mark）。
        await self.drain_durable_commands(ctl)
        pending = ctl.drain_instructions()
        for instr in pending:
            self.stage_ctx.emit(
                ev_run_instruction_received(run_id=self.run_id, instruction=instr)
            )
            self.stage_ctx.pending_instructions.append(instr)
        if ctl.is_paused():
            self.stage_ctx.emit(ev_run_paused(run_id=self.run_id, reason="user"))
            while ctl.is_paused():
                if ctl.cancel.is_set():
                    break
                if guard is None:
                    await asyncio.sleep(0.1)
                else:
                    try:
                        async with asyncio.timeout(guard.remaining_seconds):
                            await asyncio.sleep(0.1)
                    except TimeoutError as exc:
                        raise BudgetExceeded(
                            f"time budget ({guard.limits.max_runtime_seconds}s) exceeded"
                        ) from exc
                    guard.check()
            self.stage_ctx.emit(ev_run_resumed(run_id=self.run_id))

    async def drain_durable_commands(self, ctl: Any) -> None:
        """Claim + apply 本 run 的持久 RunCommand（B8）。

        把命令类型映射到进程内的 RunControl。每条命令恰好被应用/失败一次。
        best-effort：存储失败绝不打断执行。
        """
        from app.agents.db_mutation import db_mutation_scope
        from app.agents.workflow.repository import CommandStore

        try:
            factory = self.stage_ctx.persistence_session_factory
            async with db_mutation_scope(self.stage_ctx.persistence_lock):
                async with factory() as session:
                    store = CommandStore(session)
                    commands = await store.claim_pending(self.run_id)
                    for cmd in commands or []:
                        ctype = cmd.command_type
                        payload = dict(cmd.payload or {})
                        try:
                            if ctype == "pause":
                                ctl.pause()
                            elif ctype == "resume":
                                ctl.resume()
                            elif ctype == "cancel":
                                ctl.cancel.set()
                            elif ctype == "instruction":
                                text = str(payload.get("text") or "").strip()
                                if text:
                                    ctl.add_instruction(text)
                            elif ctype in ("approve", "reject"):
                                # 由审批总线消费，不在这里处理。撤回 claim 让
                                # 它自己的消费者仍能找到这条 pending 行。
                                cmd.status = "pending"
                                cmd.claimed_at = None
                                cmd.claimed_by = None
                                await session.flush()
                                continue
                            await store.mark_applied(cmd.id)
                        except Exception as exc:
                            await store.mark_failed(cmd.id, str(exc)[:500])
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("durable command drain failed", exc_info=True)
```

在 `run_environment.py` 顶部补 import：

```python
from app.agents.schemas import (
    AgentTurnContext,
    ev_run_instruction_received,
    ev_run_paused,
    ev_run_resumed,
)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_run_environment_controls.py -q --tb=short
```
Expected: 6 passed

- [ ] **Step 5: 让 `CrewAIRuntime` 改为委托**

在 `crewai_runtime.py` 里，把 `_walk_stages` 中的：

```python
            await self._respect_controls(ctx, env)
```
改为：
```python
            await env.respect_controls()
```

然后**删除** `CrewAIRuntime._respect_controls` 与 `CrewAIRuntime._drain_durable_commands` 两个方法（已迁到 `RunEnvironment`）。

- [ ] **Step 6: 运行既有控制测试确认无回归**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_agent_graph_lifecycle.py tests/test_durable_controls.py tests/test_debate.py -q --tb=short \
  --deselect tests/test_durable_controls.py::test_multi_agent_approval_pauses_then_resumes
```
Expected: 全绿

- [ ] **Step 7: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/run_environment.py backend/app/agents/runtime/crewai_runtime.py backend/tests/test_run_environment_controls.py
git commit -m "feat(agents): 运行控制下沉到 RunEnvironment，两条 walker 共享"
```

---

## Task 4: 引擎接入可中断的 `before_step`

**背景**：`on_step_start` 是只读观测（异常被吞）。暂停/取消必须**能中断**执行，所以需要一个语义不同的钩子。

**Files:**
- Modify: `backend/app/agents/workflow/engine.py`
- Modify: `backend/app/agents/orchestrator.py`
- Test: `backend/tests/test_workflow_engine_before_step.py`（新建）

**Interfaces:**
- Consumes: Task 3 的 `RunEnvironment.respect_controls`
- Produces: `WorkflowEngine(..., before_step: Callable[[str], Awaitable[None]] | None = None)`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_workflow_engine_before_step.py`：

```python
"""before_step：可中断的步骤前钩子。

与 on_step_start（只读观测，异常被吞）语义不同：before_step 抛出的异常会
终止本次执行 —— 暂停阻塞与取消都依赖它。
"""
from __future__ import annotations

import asyncio

from app.agents.workflow.engine import WorkflowEngine
from app.agents.workflow.executor import RecordingExecutor
from app.agents.workflow.schemas import Plan, Step


async def test_before_step_called_for_every_step():
    seen: list[str] = []

    async def before(step_id: str) -> None:
        seen.append(step_id)

    engine = WorkflowEngine(
        executor=RecordingExecutor(outputs={"a": "x", "b": "y"}),
        before_step=before,
    )
    await engine.run(
        Plan(version=1, goal="g",
             steps=[Step(id="a"), Step(id="b", dependencies=["a"])])
    )
    assert seen == ["a", "b"]


async def test_before_step_cancelled_error_aborts_the_run():
    async def before(step_id: str) -> None:
        if step_id == "b":
            raise asyncio.CancelledError()

    engine = WorkflowEngine(
        executor=RecordingExecutor(outputs={"a": "x", "b": "y"}),
        before_step=before,
    )
    try:
        result = await engine.run(
            Plan(version=1, goal="g",
                 steps=[Step(id="a"), Step(id="b", dependencies=["a"])])
        )
    except asyncio.CancelledError:
        return  # 预期：取消向上传播
    # 或者引擎把它归为 step 失败
    assert result.status == "failed"


async def test_before_step_other_exceptions_mark_step_failed():
    async def before(step_id: str) -> None:
        raise RuntimeError("control plane error")

    engine = WorkflowEngine(
        executor=RecordingExecutor(outputs={"solo": "x"}),
        before_step=before,
    )
    result = await engine.run(
        Plan(version=1, goal="g", steps=[Step(id="solo")])
    )
    assert result.status == "failed"


async def test_before_step_awaitable_is_awaited():
    """before_step 必须被 await —— 暂停阻塞依赖这一点。"""

    async def before(step_id: str) -> None:
        await asyncio.sleep(0.01)

    engine = WorkflowEngine(
        executor=RecordingExecutor(outputs={"solo": "x"}),
        before_step=before,
    )
    result = await engine.run(
        Plan(version=1, goal="g", steps=[Step(id="solo")])
    )
    assert result.status == "completed"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_workflow_engine_before_step.py -q --tb=short
```
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'before_step'`

- [ ] **Step 3: 实现**

在 `WorkflowEngine.__init__` 的 `on_step_retry: Any = None,` 之后加：

```python
        before_step: Any = None,
```
方法体加：

```python
        self._before_step = before_step
```

在 `run_step` 内、依赖等待之后、`if step.skip:` 之前插入：

```python
            if self._before_step is not None:
                # 与 on_step_start 不同：这里**不吞异常**。暂停阻塞与取消
                # 都依赖它能中断执行（CancelledError 必须向上传播）。
                await self._before_step(step.id)
```

- [ ] **Step 4: 引擎路径接上控制消费**

在 `orchestrator.py` 的 `_run_engine_path` 里，`WorkflowEngine(...)` 构造中加入：

```python
            before_step=lambda step_id: env.respect_controls(),
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_workflow_engine_before_step.py -q --tb=short
```
Expected: 4 passed

- [ ] **Step 6: 跑引擎相关套件**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_workflow_engine.py tests/test_workflow_replan.py tests/test_engine_routing.py -q --tb=short
```
Expected: 全绿

- [ ] **Step 7: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/workflow/engine.py backend/app/agents/orchestrator.py backend/tests/test_workflow_engine_before_step.py
git commit -m "feat(workflow): before_step 可中断钩子，引擎接入运行控制"
```

---

## Task 5: profile 名单式灰度

**Files:**
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/agents/orchestrator.py`
- Test: `backend/tests/test_engine_profile_roster.py`（新建）

**Interfaces:**
- Consumes: `Settings`、`RuntimeSelection`
- Produces:
  - `Settings.AGENT_WORKFLOW_ENGINE_PROFILES: str = ""`
  - `ChatOrchestrator._should_route_to_engine(selection) -> bool`（改为名单判定）
  - 模块级 `_engine_profiles(settings) -> frozenset[str]`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_engine_profile_roster.py`：

```python
"""名单式灰度：只有显式列出的 profile 走引擎。

安全默认：名单为空 → 没有任何 profile 走引擎，即使总开关为真。
这保证「开总开关」不会意外把所有 profile 都切过去。
"""
from __future__ import annotations

import pytest

from app.agents.orchestrator import ChatOrchestrator, _engine_profiles
from app.core.config import get_settings


class _Sel:
    def __init__(self, profile: str) -> None:
        self.multi_agent_requested = True
        self.agent_profile = profile


def test_empty_roster_routes_nothing(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "AGENT_WORKFLOW_ENGINE", "1", raising=False)
    monkeypatch.setattr(s, "AGENT_WORKFLOW_ENGINE_PROFILES", "", raising=False)
    o = ChatOrchestrator()
    assert o._should_route_to_engine(_Sel("deep_research")) is False


def test_listed_profile_routes(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "AGENT_WORKFLOW_ENGINE", "1", raising=False)
    monkeypatch.setattr(
        s, "AGENT_WORKFLOW_ENGINE_PROFILES", "deep_research", raising=False
    )
    o = ChatOrchestrator()
    assert o._should_route_to_engine(_Sel("deep_research")) is True


def test_unlisted_profile_does_not_route(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "AGENT_WORKFLOW_ENGINE", "1", raising=False)
    monkeypatch.setattr(
        s, "AGENT_WORKFLOW_ENGINE_PROFILES", "deep_research", raising=False
    )
    o = ChatOrchestrator()
    assert o._should_route_to_engine(_Sel("debate")) is False


def test_master_switch_off_wins(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "AGENT_WORKFLOW_ENGINE", "", raising=False)
    monkeypatch.setattr(
        s, "AGENT_WORKFLOW_ENGINE_PROFILES", "deep_research", raising=False
    )
    o = ChatOrchestrator()
    assert o._should_route_to_engine(_Sel("deep_research")) is False


def test_roster_parsing_is_whitespace_tolerant(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(
        s, "AGENT_WORKFLOW_ENGINE_PROFILES",
        " deep_research , parallel_research ,, debate ",
        raising=False,
    )
    assert _engine_profiles(s) == frozenset(
        {"deep_research", "parallel_research", "debate"}
    )


def test_non_multi_agent_route_never_uses_engine(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "AGENT_WORKFLOW_ENGINE", "1", raising=False)
    monkeypatch.setattr(
        s, "AGENT_WORKFLOW_ENGINE_PROFILES", "deep_research", raising=False
    )

    class _Native:
        multi_agent_requested = False
        agent_profile = "deep_research"

    o = ChatOrchestrator()
    assert o._should_route_to_engine(_Native()) is False
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_engine_profile_roster.py -q --tb=short
```
Expected: FAIL — `ImportError: cannot import name '_engine_profiles'`

- [ ] **Step 3: 加配置项**

在 `config.py` 的 `AGENT_WORKFLOW_ENGINE: str = ""` 之后插入：

```python
    # 逗号分隔的 profile 名单。总开关为真时，只有名单内的 profile 走引擎。
    # 空字符串 = 空名单 → 没有任何 profile 走引擎（安全默认：开总开关不会
    # 意外把所有 profile 都切过去）。每个 profile 可单独摘除 = 独立回滚。
    AGENT_WORKFLOW_ENGINE_PROFILES: str = ""
```

- [ ] **Step 4: 实现名单解析与路由判定**

在 `orchestrator.py` 的 `_truthy` 之前加：

```python
def _engine_profiles(settings: Any) -> frozenset[str]:
    """解析 profile 名单（逗号分隔，容忍空白与空项）。"""
    raw = getattr(settings, "AGENT_WORKFLOW_ENGINE_PROFILES", "") or ""
    return frozenset(p.strip() for p in raw.split(",") if p.strip())
```

把 `_should_route_to_engine` 整个方法替换为：

```python
    def _should_route_to_engine(self, selection: RuntimeSelection) -> bool:
        """True 当且仅当：总开关为真、本回合真的要多 Agent、且 profile 在名单内。

        名单为空 → 一律不走引擎（安全默认）。每个 profile 可单独摘除，
        摘除后立刻回到已验证的 CrewAI 路径。
        """
        settings = get_settings()
        if not _truthy(getattr(settings, "AGENT_WORKFLOW_ENGINE", "")):
            return False
        if not selection.multi_agent_requested:
            return False
        return selection.agent_profile in _engine_profiles(settings)
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_engine_profile_roster.py -q --tb=short
```
Expected: 6 passed

- [ ] **Step 6: 跑既有路由测试**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_engine_routing.py -q --tb=short
```
Expected: 全绿（注意：既有测试用的是 `AGENT_WORKFLOW_ENGINE=1` 但**没设名单**，所以它们的 `_patch_flag` 辅助函数需要同步更新——见 Step 7）

- [ ] **Step 7: 更新既有测试的 flag 辅助**

`tests/test_engine_routing.py` 里的 `_patch_flag` 目前只设 `AGENT_WORKFLOW_ENGINE`。改为同时设名单，否则引擎路径测试会全部失败：

```python
def _patch_flag(
    monkeypatch, *, engine: str = "", crewai: bool = True,
    profiles: str = "deep_research",
) -> None:
    """Patch the cached Settings instance (the orchestrator reads get_settings())."""
    s = get_settings()
    monkeypatch.setattr(s, "AGENT_WORKFLOW_ENGINE", engine, raising=False)
    monkeypatch.setattr(s, "AGENT_WORKFLOW_ENGINE_PROFILES", profiles, raising=False)
    monkeypatch.setattr(s, "CREWAI_ENABLED", crewai, raising=False)
```

- [ ] **Step 8: 重跑路由测试**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_engine_routing.py tests/test_engine_profile_roster.py -q --tb=short
```
Expected: 全绿

- [ ] **Step 9: 让引擎路径按 profile 选拓扑（不再硬编码 deep_research）**

`_run_engine_path` 目前写死了 `build_deep_research_plan` / `build_deep_research_graph`（`orchestrator.py:383,392,397,401`）。名单里加入 `parallel_research` / `debate` 后，这会让它们**顶着引擎的外壳跑 deep_research 的拓扑** —— 面板显示的节点与实际执行的 agent 对不上（`_build_stage_adapter` 仍会去 `build_research_stages` 取 stage，而 plan 里是另一套 id，直接 KeyError）。

把 `_run_engine_path` 的拓扑来源改为按 profile 分派。找到：

```python
        from app.agents.graph import build_deep_research_graph
```
```python
        from app.agents.workflow.planner import build_deep_research_plan
```
```python
        question = ctx.user_content or ""
        plan = build_deep_research_plan(question)
        # Reuse the static deep_research topology for the agent_graph event
        # (the SAME graph build_research_stages would produce). This does not
        # import crewai, so the engine path is unit-testable without it.
        graph = build_deep_research_graph(question)
```

整体替换为：

```python
        from app.agents.graph import graph_from_plan
        from app.agents.workflow.planner import build_plan_for_profile

        question = ctx.user_content or ""
        # profile 的唯一权威来源是 RuntimeSelection（orchestrator 在 stream()
        # 里写进 ctx.extra）。**不要**用 run.flow_name —— 那一列在 _create_run
        # 里写死成 "native_chat"，引擎路径从不更新它，用它会让所有 profile
        # 静默退化成 deep_research。
        selection = ctx.extra.get("runtime_selection")
        profile = getattr(selection, "agent_profile", None) or "deep_research"
        plan = build_plan_for_profile(profile, question)
        # 拓扑由 plan 推导 —— 与 walker 的静态 builder 等价（test_graph_from_plan
        # 对三个 profile 都有断言）。不再 import 具体的 builder，否则每加一个
        # profile 都要改这里。
        graph = graph_from_plan(plan)
```

同时 `_build_stage_adapter` 也要按 profile 取 stage。找到：

```python
        from app.agents.crews import build_research_stages
```
```python
        _, stages = build_research_stages(
            llm=llm, tools=[], question=ctx.user_content or ""
        )
```

替换为：

```python
        from app.agents.crews import (
            build_debate_stages,
            build_parallel_research_stages,
            build_research_stages,
            build_task_decomposition_stages,
            build_write_review_stages,
        )

        builders = {
            "parallel_research": build_parallel_research_stages,
            "debate": build_debate_stages,
            "task_decomposition": build_task_decomposition_stages,
            "write_review": build_write_review_stages,
        }
        # 与 plan 用同一个 profile 来源（见上）—— 两处必须一致，否则 plan 里
        # 的 step id 与这里取出的 stage 对不上，直接 KeyError。
        selection = ctx.extra.get("runtime_selection")
        profile = getattr(selection, "agent_profile", None) or "deep_research"
        builder = builders.get(profile, build_research_stages)
        _, stages = builder(llm=llm, tools=[], question=ctx.user_content or "")
```

（`task_decomposition` / `write_review` 的 builder 在 Task 12/13 落地。若本任务先于它们执行，`builders` 字典里先只放已存在的三个，Task 12/13 完成后回填。）

- [ ] **Step 10: 加一条拓扑一致性测试**

追加到 `backend/tests/test_engine_routing.py`：

```python
async def test_engine_path_uses_the_route_profile_not_deep_research(
    db_session, monkeypatch
):
    """引擎路径必须按 route 的 profile 选拓扑。

    旧实现硬编码 deep_research：把它切到 parallel_research 时，面板会显示
    deep_research 的节点、而 `_build_stage_adapter` 去取 parallel_research
    的 stage —— 两边对不上，直接 KeyError。
    """
    _patch_flag(
        monkeypatch, engine="1", crewai=True,
        profiles="parallel_research",
    )
    ctx = await _seed_ctx(db_session)
    ctx.extra["route"] = RouteDecision(
        execution_mode=ExecutionMode.agent,
        agent_profile="parallel_research",
        enable_tools=True,
        use_multi_agent=True,
        mode="expert",
        requested_mode="expert",
    )
    ctx.extra["workflow_executor"] = _FakeWorkflowExecutor(
        outputs={
            "coordinator": "split",
            "web-researcher": "web evidence",
            "kb-researcher": "kb evidence",
            "analyst": "merged",
            "writer": "answer",
        }
    )
    _spy_crewai(monkeypatch)

    events = await _drive(ChatOrchestrator(), ctx)
    graph_evt = next(d for k, d in events if k == "agent_graph")
    node_ids = [n["id"] for n in graph_evt["graph"]["nodes"]]

    assert node_ids == [
        "coordinator", "web-researcher", "kb-researcher", "analyst", "writer"
    ], f"expected the parallel_research topology, got {node_ids}"

    # 反向断言：绝不能用 run.flow_name 当 profile —— 它写死为 "native_chat"，
    # 用它会让所有 profile 静默退化成 deep_research 的拓扑。
    assert "researcher" not in node_ids, (
        "fallback to deep_research means the profile source is wrong"
    )
```

- [ ] **Step 11: 运行测试确认通过**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_engine_routing.py tests/test_engine_profile_roster.py -q --tb=short
```
Expected: 全绿

- [ ] **Step 12: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/core/config.py backend/app/agents/orchestrator.py backend/tests/test_engine_profile_roster.py backend/tests/test_engine_routing.py
git commit -m "feat(agents): profile 名单式灰度路由 + 引擎按 profile 选拓扑"
```

---

## Task 6: 计划门改为「计划先行，默认不阻塞」

**背景**：`PLAN_REQUIRE_CONFIRMATION` 默认 `False`，计划只被「看」不能被「改」。改为 `True` 但**语义升级**：计划总是发布、可修改、**可阻断**，但默认路径不阻塞——用户主动上闸才进门禁。生产系统的默认行为不能让用户干等。

**Files:**
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/agents/run_environment.py`
- Modify: `backend/app/agents/runtime/crewai_runtime.py`
- Test: `backend/tests/test_plan_gate.py`（新建）

**Interfaces:**
- Consumes: `RunControl`（新增 `gate_requested` 标志）
- Produces:
  - `Settings.PLAN_REQUIRE_CONFIRMATION: bool = True`
  - `Settings.PLAN_CONFIRM_TIMEOUT_S: int = 90`
  - `RunControl.request_gate() -> None` / `gate_requested: bool`
  - `RunEnvironment.await_plan_confirmation(plan_status_getter) -> bool`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_plan_gate.py`：

```python
"""计划门：计划先行，默认不阻塞；用户主动上闸才等待确认。"""
from __future__ import annotations

import asyncio

import pytest

from app.agents.run_controls import get_or_create
from app.core.config import get_settings


def test_plan_gate_defaults():
    s = get_settings()
    assert s.PLAN_REQUIRE_CONFIRMATION is True
    assert s.PLAN_CONFIRM_TIMEOUT_S == 90


def test_run_control_gate_flag_roundtrip():
    ctl = get_or_create("gate-test")
    assert ctl.gate_requested is False
    ctl.request_gate()
    assert ctl.gate_requested is True


async def test_await_confirmation_returns_immediately_when_not_gated(db_session):
    from tests.test_run_environment import _env_with_graph

    env = await _env_with_graph(db_session)
    ctl = get_or_create(env.run_id)

    async def _never_confirmed() -> str | None:
        return "draft"

    # 用户没上闸 → 立即返回 True（不阻塞）
    ok = await asyncio.wait_for(
        env.await_plan_confirmation(_never_confirmed), timeout=0.5
    )
    assert ok is True
    assert ctl.gate_requested is False


async def test_await_confirmation_waits_when_gated(db_session, monkeypatch):
    from tests.test_run_environment import _env_with_graph

    monkeypatch.setattr(
        get_settings(), "PLAN_CONFIRM_TIMEOUT_S", 2, raising=False
    )
    env = await _env_with_graph(db_session)
    ctl = get_or_create(env.run_id)
    ctl.request_gate()

    confirmed = {"v": False}

    async def _status() -> str | None:
        return "confirmed" if confirmed["v"] else "draft"

    task = asyncio.create_task(env.await_plan_confirmation(_status))
    await asyncio.sleep(0.1)
    assert not task.done(), "已上闸时应等待"

    confirmed["v"] = True
    ok = await asyncio.wait_for(task, timeout=3.0)
    assert ok is True


async def test_await_confirmation_times_out_and_proceeds(db_session, monkeypatch):
    from tests.test_run_environment import _env_with_graph

    monkeypatch.setattr(
        get_settings(), "PLAN_CONFIRM_TIMEOUT_S", 1, raising=False
    )
    env = await _env_with_graph(db_session)
    ctl = get_or_create(env.run_id)
    ctl.request_gate()

    async def _status() -> str | None:
        return "draft"

    ok = await asyncio.wait_for(env.await_plan_confirmation(_status), timeout=4.0)
    # 超时后按默认计划继续（用户没响应不应导致任务失败）
    assert ok is False


async def test_await_confirmation_honors_cancel(db_session, monkeypatch):
    from tests.test_run_environment import _env_with_graph

    monkeypatch.setattr(
        get_settings(), "PLAN_CONFIRM_TIMEOUT_S", 30, raising=False
    )
    env = await _env_with_graph(db_session)
    ctl = get_or_create(env.run_id)
    ctl.request_gate()

    async def _status() -> str | None:
        return "draft"

    task = asyncio.create_task(env.await_plan_confirmation(_status))
    await asyncio.sleep(0.1)
    ctl.cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_plan_gate.py -q --tb=short
```
Expected: FAIL — `AttributeError: 'RunControl' object has no attribute 'gate_requested'`

- [ ] **Step 3: 配置项改默认值**

在 `config.py` 里：

```python
    PLAN_REQUIRE_CONFIRMATION: bool = False
```
改为：
```python
    PLAN_REQUIRE_CONFIRMATION: bool = True
```

并把 `PLAN_CONFIRM_TIMEOUT_S: int = 300` 改为：

```python
    PLAN_CONFIRM_TIMEOUT_S: int = 90
```

同时更新该配置上方注释，说明新语义（计划先行、默认不阻塞、用户可上闸）。

- [ ] **Step 4: 给 `RunControl` 加上闸标志**

在 `run_controls.py` 的 `RunControl` 里，`instructions` 字段之后加：

```python
    # 用户主动请求计划门禁（「暂停执行」按钮）。默认 False = 计划先行不阻塞。
    gate_requested: bool = False
```

并在 `drain_instructions` 之后加方法：

```python
    def request_gate(self) -> None:
        """用户请求在下一个 step 边界进入计划门禁。"""
        self.gate_requested = True

    def clear_gate(self) -> None:
        self.gate_requested = False
```

- [ ] **Step 5: 在 `RunEnvironment` 实现门禁**

在 `drain_durable_commands` 之后插入：

```python
    async def await_plan_confirmation(self, plan_status_getter: Any) -> bool:
        """等用户确认计划，或超时/未上闸时立即返回。

        ``plan_status_getter`` 是一个 async 无参可调用，返回 ``AgentRun.
        plan_status``（``draft`` / ``confirmed`` / ``updated``）。

        返回 True 表示「可以继续执行」：
          * 用户没上闸 → 立即 True（默认路径零等待）
          * 用户上闸且确认/修改了计划 → True
          * 超时 → False（调用方按默认计划继续，并应发出说明）
        """
        from app.agents.run_controls import get_or_create as _get_or_create

        ctl = self.ctx.extra.get("run_control") or _get_or_create(self.run_id)
        if ctl is None or not ctl.gate_requested:
            return True
        from app.core.config import get_settings

        timeout_s = int(getattr(get_settings(), "PLAN_CONFIRM_TIMEOUT_S", 90))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if ctl.cancel.is_set():
                raise asyncio.CancelledError()
            try:
                status = await plan_status_getter()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("plan-status poll failed", exc_info=True)
                status = None
            if status in ("confirmed", "updated"):
                ctl.clear_gate()
                return True
            await asyncio.sleep(0.5)
        return False
```

- [ ] **Step 6: 运行测试确认通过**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_plan_gate.py -q --tb=short
```
Expected: 6 passed

- [ ] **Step 7: 让 walker 与引擎都接上门禁**

在 `crewai_runtime.py` 里，把 `_await_plan_confirmation` 的调用点改为使用新方法（保留原有轮询实现作为 `plan_status_getter`）。具体：把

```python
            if _plan_gate:
                gated = await self._await_plan_confirmation(ctx, stage_ctx)
```

改为：

```python
            if _plan_gate:
                async def _status() -> str | None:
                    async with db_mutation_scope(stage_ctx.persistence_lock):
                        factory = stage_ctx.persistence_session_factory
                        async with factory() as session:
                            row = await session.execute(
                                select(AgentRun.plan_status).where(
                                    AgentRun.id == ctx.run_id
                                )
                            )
                            return row.scalar_one_or_none()

                gated = await env.await_plan_confirmation(_status)
```

并在引擎路径 `_run_engine_path` 中，`plan = build_deep_research_plan(question)` 之后、`env.begin()` 之前插入同样的门禁调用（引擎路径的 plan 已在内存中，`plan_status_getter` 复用同一实现）。

- [ ] **Step 8: 运行相关套件**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_plan_gate.py tests/test_agent_phase1.py tests/test_engine_routing.py tests/test_durable_controls.py -q --tb=short \
  --deselect tests/test_durable_controls.py::test_multi_agent_approval_pauses_then_resumes
```
Expected: 全绿

- [ ] **Step 9: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/core/config.py backend/app/agents/run_controls.py backend/app/agents/run_environment.py backend/app/agents/runtime/crewai_runtime.py backend/app/agents/orchestrator.py backend/tests/test_plan_gate.py
git commit -m "feat(agents): 计划门改为计划先行默认不阻塞，用户可主动上闸"
```

---

## Task 7: 辩论 UI 入口

**背景**：后端 `VALID_MODES`、`decide_route` 的 `debate` 分支、`crews/debate.py` **全部已存在且完整**。前端 `user-modes.ts` 只暴露 `speed/expert/hermes`，且 `mapLegacyMode` 把历史的 `debate` 值映成了 `expert`。本任务是纯前端补入口。

**Files:**
- Modify: `frontend/src/lib/user-modes.ts`
- Modify: `frontend/src/stores/chat-ui-store.ts`
- Modify: `frontend/src/components/agents/agent-run-header.tsx`
- Test: `frontend/src/lib/__tests__/user-modes.test.ts`（追加）

**Interfaces:**
- Produces: `USER_MODES` 增加 `debate` 项；`SPECIAL_MODES` 含 `debate`；`mapLegacyMode("debate") === "debate"`

- [ ] **Step 1: 写失败的测试**

追加到 `frontend/src/lib/__tests__/user-modes.test.ts`：

```ts
describe("辩论模式", () => {
  it("出现在模式选择器里", () => {
    const debate = USER_MODES.find((m) => m.value === "debate");
    expect(debate).toBeDefined();
    expect(debate?.label).toContain("辩论");
  });

  it("被标记为特殊模式（composer 显示徽章）", () => {
    expect(isSpecialMode("debate")).toBe(true);
  });

  it("与 expert 一样是多 Agent 模式", () => {
    expect(isSpecialMode("expert")).toBe(true);
    expect(isSpecialMode("speed")).toBe(false);
  });

  it("极速模式不是特殊模式", () => {
    expect(isSpecialMode("hermes")).toBe(false);
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd frontend && npx vitest run src/lib/__tests__/user-modes.test.ts
```
Expected: FAIL — `debate` 未定义

- [ ] **Step 3: 加模式定义**

在 `user-modes.ts` 的 `USER_MODES` 数组中，`expert` 之后插入：

```ts
  {
    value: "debate",
    label: "辩论模式",
    short: "辩论",
    description:
      "两个 Agent 并行论证对立立场，第三个 Agent 担任中立裁判给出条件化结论。适合需要正反权衡的选择题。",
    icon: Scale,
  },
```

并在 import 中加 `Scale`：

```ts
import { Bot, Brain, Scale, Zap, type LucideIcon } from "lucide-react";
```

把 `SPECIAL_MODES` 改为：

```ts
export const SPECIAL_MODES: ReadonlySet<string> = new Set(["expert", "debate"]);
```

并在文档注释里补上第 4 个模式的说明。

- [ ] **Step 4: 修正 legacy 映射**

在 `chat-ui-store.ts` 的 `mapLegacyMode` 里把：

```ts
  if (v === "deep_research" || v === "debate") return "expert"; // was multi-agent
```
改为：
```ts
  if (v === "debate") return "debate"; // 辩论现在是独立可选的模式
  if (v === "deep_research") return "expert";
```

- [ ] **Step 5: 加 profile 标签**

在 `agent-run-header.tsx` 的 profile 标签映射里（找 `debate: "辩论"` 所在对象），确认三个新 profile 都有标签。若无则补：

```ts
    task_decomposition: "任务分解",
    write_review: "写-审-改",
```

- [ ] **Step 6: 运行测试确认通过**

```bash
cd frontend && npx vitest run src/lib/__tests__/user-modes.test.ts src/stores/__tests__/chat-ui-store.test.ts
```
Expected: 全绿

- [ ] **Step 7: 全量前端门禁**

```bash
cd frontend && npm run typecheck && npm run lint && npm run test
```
Expected: 全绿（`chat-ui-store.test.ts` 里若有断言 "v3 debate → expert" 需同步改为 → debate）

- [ ] **Step 8: 提交**

```bash
cd /d/Gitee/MyGPT && git add frontend/src/lib/user-modes.ts frontend/src/stores/chat-ui-store.ts frontend/src/components/agents/agent-run-header.tsx frontend/src/lib/__tests__/user-modes.test.ts
git commit -m "feat(frontend): 辩论模式入口 + 新 profile 标签"
```

---

## Task 8: 前端消费重试状态与计划门

**Files:**
- Modify: `frontend/src/lib/agent-graph-types.ts`
- Modify: `frontend/src/lib/agent-graph-reducer.ts`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/components/agents/plan-review.tsx`
- Test: `frontend/src/lib/__tests__/agent-graph-reducer.test.ts`（追加）

**Interfaces:**
- Consumes: 后端 Task 1 的 `agent_status.retrying` 载荷
- Produces: `AgentGraphNode.retrying?: { attempt: number; error: string }`

- [ ] **Step 1: 写失败的测试**

追加到 `frontend/src/lib/__tests__/agent-graph-reducer.test.ts`：

```ts
describe("重试状态", () => {
  const base = (): AgentGraphState => ({
    runId: "r1", runtime: "crewai", flowName: "deep_research",
    mode: "sequential", status: "running",
    nodes: [{ id: "researcher", name: "Researcher", role: "资料检索",
              stage: 0, status: "running" }],
    edges: [], activeAgentIds: ["researcher"],
  });

  it("failed 节点可被 AGENT_STATUS 翻回 running（重试）", () => {
    const state = base();
    state.nodes[0].status = "failed";
    const next = reducer(state, {
      type: "AGENT_STATUS", runId: "r1", agentId: "researcher",
      patch: { status: "running", retrying: { attempt: 2, error: "timeout" } },
    });
    expect(next.nodes[0].status).toBe("running");
    expect(next.nodes[0].retrying).toEqual({ attempt: 2, error: "timeout" });
  });

  it("completed 时清除 retrying 与 error", () => {
    const state = base();
    state.nodes[0].status = "failed";
    state.nodes[0].retrying = { attempt: 2, error: "timeout" };
    state.nodes[0].error = "timeout";
    const next = reducer(state, {
      type: "AGENT_STATUS", runId: "r1", agentId: "researcher",
      patch: { status: "completed" },
    });
    expect(next.nodes[0].status).toBe("completed");
  });

  it("cancelled 节点不被 running 覆盖", () => {
    const state = base();
    state.nodes[0].status = "cancelled";
    const next = reducer(state, {
      type: "AGENT_STATUS", runId: "r1", agentId: "researcher",
      patch: { status: "running" },
    });
    expect(next.nodes[0].status).toBe("cancelled");
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd frontend && npx vitest run src/lib/__tests__/agent-graph-reducer.test.ts
```
Expected: FAIL — `retrying` 不在类型中 / 状态转换被 `canTransitionTo` 挡住

- [ ] **Step 3: 加类型**

在 `agent-graph-types.ts` 的 `AgentGraphNode` 里，`costUsd` 之后插入：

```ts
  /** 正在重试（引擎 transient 重试）：{attempt, error}。 */
  retrying?: { attempt: number; error: string };
```

- [ ] **Step 4: 允许 failed → running 转换**

在 `agent-graph-types.ts` 的 `canTransitionTo` 里，确认 `failed` 可以转 `running`（重试语义）。若当前不允许，加入：

```ts
  // failed → running：引擎的 transient 重试会复用同一节点。
  failed: ["running", "waiting", "completed", "cancelled"],
```

- [ ] **Step 5: 在 reducer 的 AGENT_STATUS 里传递 retrying**

确认 `AGENT_STATUS` 的 patch 通道会把 `retrying` 一起写入（它走通用的 `{...n, ...action.patch}` 展开，无需额外代码）。若 `mergeNonStatus` 需要保留 `retrying`，确认它不被排除。

- [ ] **Step 6: api.ts 传递 retrying**

在 `api.ts` 的 `agent_status` 分派里，`costUsd: data.cost_usd,` 之后加：

```ts
            retrying: data.retrying,
```

并在 handler 类型定义里补 `retrying?: { attempt: number; error: string };`。

- [ ] **Step 7: 运行测试确认通过**

```bash
cd frontend && npx vitest run src/lib/__tests__/agent-graph-reducer.test.ts
```
Expected: 全绿

- [ ] **Step 8: PlanReview 文案更新**

在 `plan-review.tsx` 里，当 `status === "draft"` 且门开着时，把当前误导性的「等待确认」改为准确的两态文案：

- 未上闸：显示「计划已发布 · 执行中（可随时暂停修改）」
- 已上闸（`gate_requested`）：显示「等待你的确认」

- [ ] **Step 9: 全量前端门禁**

```bash
cd frontend && npm run typecheck && npm run lint && npm run test
```
Expected: 全绿

- [ ] **Step 10: 提交**

```bash
cd /d/Gitee/MyGPT && git add frontend/src/lib/agent-graph-types.ts frontend/src/lib/agent-graph-reducer.ts frontend/src/lib/api.ts frontend/src/components/agents/plan-review.tsx frontend/src/lib/__tests__/agent-graph-reducer.test.ts
git commit -m "feat(frontend): 重试状态可见 + 计划门两态文案"
```

---

## Task 9: 可观测性接入

**Files:**
- Modify: `backend/app/agents/orchestrator.py`
- Modify: `backend/app/agents/workflow/engine.py`
- Test: `backend/tests/test_agent_observability.py`（新建）

- [ ] **Step 5: 运行测试确认通过**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_agent_observability.py -q --tb=short
```
Expected: 3 passed

- [ ] **Step 6: 确认脱敏未被破坏**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_observability_redaction.py tests/test_observability_instrumentation.py -q --tb=short
```
Expected: 全绿

- [ ] **Step 7: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/orchestrator.py backend/app/agents/workflow/engine.py backend/tests/test_agent_observability.py
git commit -m "feat(agents): 引擎灰度与重试的指标可见性"
```

---

## Task 10: LLM 规划器

**背景**：规划器目前是纯模板（`planner.py`，无任何 LLM 入口）。本任务新增一个**带确定性回退**的 LLM 规划器：模型产出的 plan 必须通过 `validate_plan()`，否则回退模板。**LLM 永远不能让引擎挂掉。**

**Files:**
- Create: `backend/app/agents/workflow/llm_planner.py`
- Modify: `backend/app/core/config.py`
- Test: `backend/tests/test_llm_planner.py`（新建）

**Interfaces:**
- Consumes: `ModelProvider.chat`（`app.providers.base`）、`ChatOptions`/`ChatResult`、`build_plan_for_profile`/`validate_plan`（`app.agents.workflow.planner`）、`BudgetGuard`
- Produces:
  - `async def build_plan_with_llm(*, provider, model_config, profile, question, guard, max_steps) -> Plan`
  - `_parse_plan_json(raw: str, profile: str, question: str) -> Plan | None`（纯函数，可单测）
  - `Settings.AGENT_LLM_PLANNER: bool = False`、`AGENT_LLM_PLANNER_MAX_STEPS: int = 8`、`AGENT_LLM_PLANNER_TIMEOUT_S: float = 8.0`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_llm_planner.py`：

```python
"""LLM 规划器：模型可以提议，但模板始终是回退。

核心保证：**模型产出非法或调用失败时，运行绝不中断** —— 一律回退到
build_plan_for_profile 的模板 plan。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.agents.workflow.llm_planner import _parse_plan_json, build_plan_with_llm
from app.agents.workflow.planner import build_plan_for_profile


class _StubProvider:
    def __init__(self, content: str, *, fail: Exception | None = None) -> None:
        self.content = content
        self.fail = fail
        self.calls = 0

    async def chat(self, messages, options=None):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(
            content=self.content,
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            finish_reason="stop",
        )


_VALID = """
{
  "steps": [
    {"id": "researcher", "role": "researcher", "name": "Researcher",
     "task_description": "gather evidence", "dependencies": []},
    {"id": "analyst", "role": "analyst", "name": "Analyst",
     "task_description": "cross-check", "dependencies": ["researcher"]},
    {"id": "writer", "role": "writer", "name": "Writer",
     "task_description": "write the answer", "dependencies": ["analyst"]}
  ]
}
"""


def test_parse_plan_json_accepts_valid_payload():
    plan = _parse_plan_json(_VALID, "deep_research", "q")
    assert plan is not None
    assert plan.step_ids == ["researcher", "analyst", "writer"]
    assert plan.get("analyst").dependencies == ["researcher"]


def test_parse_plan_json_strips_markdown_fence():
    fenced = f"```json\n{_VALID}\n```"
    plan = _parse_plan_json(fenced, "deep_research", "q")
    assert plan is not None
    assert plan.step_ids == ["researcher", "analyst", "writer"]


def test_parse_plan_json_rejects_cycle():
    bad = """
    {"steps": [
      {"id": "a", "dependencies": ["b"]},
      {"id": "b", "dependencies": ["a"]}
    ]}
    """
    assert _parse_plan_json(bad, "x", "q") is None


def test_parse_plan_json_rejects_missing_dependency():
    bad = """
    {"steps": [{"id": "a", "dependencies": ["nope"]}]}
    """
    assert _parse_plan_json(bad, "x", "q") is None


def test_parse_plan_json_rejects_duplicate_ids():
    bad = """
    {"steps": [{"id": "a", "dependencies": []}, {"id": "a", "dependencies": []}]}
    """
    assert _parse_plan_json(bad, "x", "q") is None


def test_parse_plan_json_rejects_malformed_json():
    assert _parse_plan_json("{not json", "x", "q") is None


def test_parse_plan_json_rejects_empty_steps():
    assert _parse_plan_json('{"steps": []}', "x", "q") is None


def test_parse_plan_json_rejects_too_many_steps():
    steps = ",".join(
        f'{{"id": "s{i}", "dependencies": []}}' for i in range(50)
    )
    assert _parse_plan_json(f'{{"steps": [{steps}]}}', "x", "q") is None


async def test_build_plan_uses_llm_output_when_valid():
    provider = _StubProvider(_VALID)
    plan = await build_plan_with_llm(
        provider=provider, model_config=None, profile="deep_research",
        question="q", guard=None, max_steps=8,
    )
    assert provider.calls == 1
    assert plan.step_ids == ["researcher", "analyst", "writer"]


async def test_build_plan_falls_back_on_invalid_output():
    provider = _StubProvider('{"steps": []}')
    plan = await build_plan_with_llm(
        provider=provider, model_config=None, profile="deep_research",
        question="q", guard=None, max_steps=8,
    )
    template = build_plan_for_profile("deep_research", "q")
    assert plan.step_ids == template.step_ids


async def test_build_plan_falls_back_on_provider_error():
    provider = _StubProvider("", fail=RuntimeError("boom"))
    plan = await build_plan_with_llm(
        provider=provider, model_config=None, profile="deep_research",
        question="q", guard=None, max_steps=8,
    )
    template = build_plan_for_profile("deep_research", "q")
    assert plan.step_ids == template.step_ids


async def test_build_plan_falls_back_on_timeout(monkeypatch):
    class _SlowProvider:
        async def chat(self, messages, options=None):
            await asyncio.sleep(10)

    from app.core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "AGENT_LLM_PLANNER_TIMEOUT_S", 0.05, raising=False
    )
    plan = await build_plan_with_llm(
        provider=_SlowProvider(), model_config=None, profile="deep_research",
        question="q", guard=None, max_steps=8,
    )
    template = build_plan_for_profile("deep_research", "q")
    assert plan.step_ids == template.step_ids


async def test_build_plan_skips_llm_when_budget_exhausted():
    """预算耗尽时直接回退模板，不发起调用。"""
    from app.agents.policies import BudgetExceeded, BudgetGuard, BudgetLimits

    guard = BudgetGuard(BudgetLimits(max_total_tokens=1))
    guard.add_usage({"total_tokens": 999})

    provider = _StubProvider(_VALID)
    with pytest.raises(BudgetExceeded):
        guard.check()  # 确认预算确实已耗尽

    plan = await build_plan_with_llm(
        provider=provider, model_config=None, profile="deep_research",
        question="q", guard=guard, max_steps=8,
    )
    assert provider.calls == 0, "预算耗尽时不得发起模型调用"
    assert plan.step_ids == build_plan_for_profile("deep_research", "q").step_ids


async def test_llm_planner_flags_default_off():
    from app.core.config import get_settings

    s = get_settings()
    assert s.AGENT_LLM_PLANNER is False
    assert s.AGENT_LLM_PLANNER_MAX_STEPS == 8
    assert s.AGENT_LLM_PLANNER_TIMEOUT_S == 8.0
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_agent_observability.py -q --tb=short
```
Expected: FAIL — 无 counter 打点

- [ ] **Step 3: 在路由判定里打点**

在 `orchestrator.py` 的 `_should_route_to_engine` 中，`return` 之前加：

```python
        from app.observability import observe_counter

        observe_counter("agent.engine.profile", 1, profile=selection.agent_profile)
        return True
```

（只在实际走引擎时打点，否则指标会包含所有被拒绝的判定。）

- [ ] **Step 4: 引擎重试打点**

在 `engine.py` 的 transient 分支里，`observe_counter("workflow.steps", 1, outcome="retry")` 之后加：

```python
                    observe_counter("workflow.step.retry", 1, step_id=step.id)
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_agent_observability.py -q --tb=short
```
Expected: 1 passed

- [ ] **Step 6: 确认脱敏未被破坏**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_observability_redaction.py tests/test_observability_instrumentation.py -q --tb=short
```
Expected: 全绿

- [ ] **Step 7: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/orchestrator.py backend/app/agents/workflow/engine.py backend/tests/test_agent_observability.py
git commit -m "feat(agents): 引擎灰度与重试的指标可见性"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_llm_planner.py -q --tb=short
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agents.workflow.llm_planner'`

- [ ] **Step 3: 加配置项**

在 `config.py` 的 `AGENT_WORKFLOW_ENGINE_PROFILES` 之后插入：

```python
    # LLM 规划器：开启后计划先由模型提议，产出必须通过 validate_plan()，
    # 否则回退模板。模型失败/超时/预算耗尽一律回退 —— LLM 永远不能让引擎
    # 挂掉。默认关：这是额外的模型调用，消耗用户 token 且发生在首 token 之前。
    AGENT_LLM_PLANNER: bool = False
    AGENT_LLM_PLANNER_MAX_STEPS: int = 8
    # 首 token 延迟预算（总预算，不是单次）。超时即回退模板。
    AGENT_LLM_PLANNER_TIMEOUT_S: float = 8.0
    # LLM verifier：用模型验收步骤产出（而非只查 min_chars）。
    # 非法 verdict 回退 RuleBasedVerifier。默认关。
    AGENT_LLM_VERIFIER: bool = False
    AGENT_LLM_VERIFIER_TIMEOUT_S: float = 10.0
```

- [ ] **Step 4: 实现 `llm_planner.py`**

创建 `backend/app/agents/workflow/llm_planner.py`：

```python
"""LLM 规划器：模型提议计划，模板始终是回退。

这个模块**不会**让引擎依赖模型。契约是：

  * 产出必须通过 :func:`~app.agents.workflow.planner.validate_plan`
    （无环、依赖存在、id 唯一）与步数上限；
  * 任何失败 —— 模型报错、超时、预算耗尽、JSON 非法、plan 非法 ——
    一律回退 :func:`~app.agents.workflow.planner.build_plan_for_profile`。

对调用方而言，``build_plan_with_llm`` 永远返回一个可用的 Plan。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from app.agents.workflow.planner import build_plan_for_profile, validate_plan
from app.agents.workflow.schemas import Plan, Step

logger = logging.getLogger(__name__)

# 模型常把 JSON 包在 markdown 代码块里。剥掉围栏后再解析。
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

_SYSTEM_PROMPT = (
    "You are a planning component. Given a user request, produce a small "
    "execution plan as STRICT JSON. Output JSON only — no prose, no markdown "
    "fences. Schema:\n"
    '{"steps": [{"id": "<slug>", "role": "<agent role>", "name": "<display name>", '
    '"task_description": "<one sentence>", "dependencies": ["<other step id>"]}]}\n'
    "Rules: ids are unique snake_case slugs; dependencies reference existing ids; "
    "the graph must be acyclic; keep it small and only include work that is "
    "genuinely needed. Answer in the user's language for name/task_description."
)


def _strip_fence(raw: str) -> str:
    m = _FENCE_RE.match(raw or "")
    return m.group(1) if m else (raw or "")


def _parse_plan_json(raw: str, profile: str, question: str) -> Plan | None:
    """把模型输出解析成 Plan；任何不合规都返回 None（调用方回退模板）。"""
    from app.core.config import get_settings

    max_steps = int(getattr(get_settings(), "AGENT_LLM_PLANNER_MAX_STEPS", 8))
    try:
        payload = json.loads(_strip_fence(raw))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return None
    if len(raw_steps) > max_steps:
        return None

    steps: list[Step] = []
    for item in raw_steps:
        if not isinstance(item, dict):
            return None
        sid = str(item.get("id") or "").strip()
        if not sid:
            return None
        deps = item.get("dependencies") or []
        if not isinstance(deps, list):
            return None
        steps.append(
            Step(
                id=sid,
                role=str(item.get("role") or ""),
                name=str(item.get("name") or sid),
                task_description=str(item.get("task_description") or ""),
                dependencies=[str(d) for d in deps],
                acceptance_criteria={"min_chars": 1},
            )
        )

    plan = Plan(
        version=1,
        goal=(question or "").strip(),
        profile=profile,
        steps=steps,
        max_replans=1,
    )
    try:
        validate_plan(plan)
    except Exception:
        return None
    return plan


def _charge(guard: Any, usage: Any, model_config: Any, kind: str) -> None:
    """把规划器/verifier 的 token 计入 run 预算（与 stage 同源）。

    **同步调用**：BudgetGuard 的临界区用 RLock，中间不得 await。
    """
    if guard is None or not isinstance(usage, dict) or not usage:
        return
    from app.core.pricing import usage_cost

    cost = usage.get("cost_usd")
    if cost is None:
        cost = usage_cost(getattr(model_config, "model_name", None), usage)
    guard.add_usage(usage, cost_usd=cost, usage_id=f"crewai:{kind}")


async def build_plan_with_llm(
    *,
    provider: Any,
    model_config: Any,
    profile: str,
    question: str,
    guard: Any = None,
    max_steps: int = 8,
) -> Plan:
    """用模型提议一个 plan；不可用时回退模板。**永远返回可用的 Plan**。"""
    from app.agents.policies import BudgetExceeded
    from app.core.config import get_settings
    from app.observability import observe_counter, observe_span

    fallback = build_plan_for_profile(profile, question)

    if provider is None:
        observe_counter("agent.llm_planner", 1, outcome="no_provider")
        return fallback

    # 预算耗尽时不发起调用 —— 规划器不该成为压垮预算的最后一根稻草。
    if guard is not None:
        try:
            guard.check()
        except BudgetExceeded:
            observe_counter("agent.llm_planner", 1, outcome="budget_exhausted")
            logger.info("LLM planner skipped: budget exhausted")
            return fallback

    timeout_s = float(getattr(get_settings(), "AGENT_LLM_PLANNER_TIMEOUT_S", 8.0))
    from app.providers.base import ChatOptions

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Profile: {profile}\nRequest: {question}\n"
            f"Produce at most {max_steps} steps.",
        },
    ]
    options = ChatOptions(temperature=0.2, max_tokens=1024)

    try:
        with observe_span("agent.llm_plan", profile=profile):
            async with asyncio.timeout(timeout_s):
                result = await provider.chat(messages, options)
    except TimeoutError:
        observe_counter("agent.llm_planner", 1, outcome="timeout")
        logger.info("LLM planner timed out; using template plan")
        return fallback
    except asyncio.CancelledError:
        raise
    except Exception:
        observe_counter("agent.llm_planner", 1, outcome="error")
        logger.warning("LLM planner call failed; using template plan", exc_info=True)
        return fallback

    _charge(guard, getattr(result, "usage", None), model_config, "planner")

    plan = _parse_plan_json(getattr(result, "content", "") or "", profile, question)
    if plan is None:
        observe_counter("agent.llm_planner", 1, outcome="invalid_plan")
        logger.info("LLM planner produced an invalid plan; using template plan")
        return fallback

    observe_counter("agent.llm_planner", 1, outcome="ok")
    return plan
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_llm_planner.py -q --tb=short
```
Expected: 14 passed

- [ ] **Step 6: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/workflow/llm_planner.py backend/app/core/config.py backend/tests/test_llm_planner.py
git commit -m "feat(workflow): LLM 规划器（带确定性回退与延迟预算）"
```

---

## Task 11: LLM verifier

**背景**：`RuleBasedVerifier` 只校验 `min_chars`（`verifier.py:71`），是整条链路最弱的一环——它能通过一段 100 字的空话，却拦不住偏离问题的长回答。本任务加一个 LLM verifier，**非法输出一律回退规则版**。

**Files:**
- Create: `backend/app/agents/workflow/llm_verifier.py`
- Test: `backend/tests/test_llm_verifier.py`（新建）

**Interfaces:**
- Consumes: `Verifier` protocol（`app.agents.workflow.verifier`）、`VerifierResult`/`VerificationVerdict`、`RuleBasedVerifier`、`_charge`（Task 10 的 `llm_planner._charge`）
- Produces:
  - `class LLMVerifier`，构造参数 `provider`、`model_config=None`、`guard=None`、`fallback=None`
  - `_parse_verdict_json(raw: str, plan: Plan) -> VerifierResult | None`（纯函数）

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_llm_verifier.py`：

```python
"""LLM verifier：模型验收步骤产出，非法输出一律回退规则版。

核心保证：**verifier 永远返回一个合法 verdict**。模型胡说、超时、报错时，
运行不该因为验收环节而中断。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.agents.workflow.llm_verifier import LLMVerifier, _parse_verdict_json
from app.agents.workflow.planner import build_deep_research_plan
from app.agents.workflow.schemas import (
    Plan,
    StepObservation,
    VerificationVerdict,
)


class _StubProvider:
    def __init__(self, content: str, *, fail: Exception | None = None) -> None:
        self.content = content
        self.fail = fail
        self.calls = 0

    async def chat(self, messages, options=None):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(
            content=self.content,
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            finish_reason="stop",
        )


def _plan() -> Plan:
    return build_deep_research_plan("q")


def _obs(output: str = "充分的分析结论" * 10) -> dict[str, StepObservation]:
    return {
        "researcher": StepObservation(step_id="researcher", output=output),
        "analyst": StepObservation(step_id="analyst", output=output),
        "writer": StepObservation(step_id="writer", output=output),
    }


def test_parse_verdict_accepts_pass():
    r = _parse_verdict_json(
        '{"verdict": "pass", "findings": ["looks good"]}', _plan()
    )
    assert r is not None
    assert r.verdict == VerificationVerdict.pass_


def test_parse_verdict_accepts_revise_with_known_steps():
    r = _parse_verdict_json(
        '{"verdict": "revise", "findings": ["too thin"], '
        '"revise_step_ids": ["analyst"]}',
        _plan(),
    )
    assert r is not None
    assert r.verdict == VerificationVerdict.revise
    assert r.revise_step_ids == ["analyst"]


def test_parse_verdict_rejects_unknown_step_id():
    r = _parse_verdict_json(
        '{"verdict": "revise", "revise_step_ids": ["does-not-exist"]}', _plan()
    )
    assert r is None, "未知 step id 必须被拒（否则 revise 会空转）"


def test_parse_verdict_rejects_revise_without_steps():
    assert _parse_verdict_json('{"verdict": "revise"}', _plan()) is None


def test_parse_verdict_rejects_unknown_verdict():
    assert _parse_verdict_json('{"verdict": "maybe"}', _plan()) is None


def test_parse_verdict_rejects_malformed_json():
    assert _parse_verdict_json("{oops", _plan()) is None


def test_parse_verdict_strips_markdown_fence():
    r = _parse_verdict_json(
        '```json\n{"verdict": "fail", "findings": ["事实错误"]}\n```',
        _plan(),
    )
    assert r is not None
    assert r.verdict == VerificationVerdict.fail


async def test_verifier_uses_model_verdict():
    v = LLMVerifier(
        provider=_StubProvider('{"verdict": "pass", "findings": []}'),
        model_config=None,
    )
    result = await v.verify(_plan(), _obs())
    assert result.verdict == VerificationVerdict.pass_


async def test_verifier_falls_back_on_invalid_output():
    v = LLMVerifier(
        provider=_StubProvider('{"verdict": "nonsense"}'), model_config=None
    )
    result = await v.verify(_plan(), _obs())
    # 规则版对足够长的产出给 pass。
    assert result.verdict == VerificationVerdict.pass_


async def test_verifier_falls_back_on_error():
    v = LLMVerifier(
        provider=_StubProvider("", fail=RuntimeError("boom")), model_config=None
    )
    result = await v.verify(_plan(), _obs())
    assert result.verdict == VerificationVerdict.pass_


async def test_verifier_falls_back_on_timeout(monkeypatch):
    class _Slow:
        async def chat(self, messages, options=None):
            await asyncio.sleep(10)

    from app.core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "AGENT_LLM_VERIFIER_TIMEOUT_S", 0.05, raising=False
    )
    v = LLMVerifier(provider=_Slow(), model_config=None)
    result = await v.verify(_plan(), _obs())
    assert result.verdict == VerificationVerdict.pass_


async def test_verifier_falls_back_to_revise_for_thin_output():
    """规则版仍然生效：产出过短时回退结果是 revise 而非 pass。"""
    v = LLMVerifier(provider=_StubProvider("{bad"), model_config=None)
    result = await v.verify(_plan(), _obs(output=""))
    assert result.verdict == VerificationVerdict.revise


async def test_verifier_skips_model_when_observations_empty():
    """没有观测可验收时不该浪费一次模型调用。"""
    provider = _StubProvider('{"verdict": "pass"}')
    v = LLMVerifier(provider=provider, model_config=None)
    result = await v.verify(_plan(), {})
    assert provider.calls == 0
    assert result.verdict == VerificationVerdict.revise


async def test_verifier_rejects_unknown_step_in_plan():
    """模型给出的 revise_step_ids 含未知 id 时回退，而不是传给 planner。"""
    v = LLMVerifier(
        provider=_StubProvider(
            '{"verdict": "revise", "revise_step_ids": ["ghost"]}'
        ),
        model_config=None,
    )
    result = await v.verify(_plan(), _obs())
    assert result.verdict == VerificationVerdict.pass_, "应回退到规则版"


def test_llm_verifier_flag_defaults_off():
    from app.core.config import get_settings

    s = get_settings()
    assert s.AGENT_LLM_VERIFIER is False
    assert s.AGENT_LLM_VERIFIER_TIMEOUT_S == 10.0
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_llm_verifier.py -q --tb=short
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agents.workflow.llm_verifier'`

- [ ] **Step 3: 实现 `llm_verifier.py`**

创建 `backend/app/agents/workflow/llm_verifier.py`：

```python
"""LLM verifier：模型验收步骤产出，非法输出一律回退规则版。

:class:`~app.agents.workflow.verifier.RuleBasedVerifier` 只校验
``min_chars`` —— 它能放过一段 100 字的空话，拦不住偏离问题的长回答。
本类让模型按每步的 ``acceptance_criteria`` 与产出正文做判断。

**契约**：``verify`` 永远返回一个合法 verdict。模型报错、超时、输出非法、
``revise_step_ids`` 含未知 step —— 全部回退 ``RuleBasedVerifier``。
运行绝不因为验收环节而中断。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from app.agents.workflow.schemas import (
    Plan,
    StepObservation,
    VerificationVerdict,
    VerifierResult,
)
from app.agents.workflow.verifier import RuleBasedVerifier

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

_SYSTEM_PROMPT = (
    "You are a verification component. You are given a plan's steps and each "
    "step's produced output. Judge whether the outputs satisfy the work they "
    "were asked to do. Output STRICT JSON only — no prose, no code fences.\n"
    'Schema: {"verdict": "pass"|"revise"|"fail", "findings": ["..."], '
    '"revise_step_ids": ["<step id>"]}\n'
    "Use revise when specific steps need reworking (list them in "
    "revise_step_ids). Use fail only for an unrecoverable problem. Judge the "
    "outputs, not the process. Do not invent step ids that are not in the plan."
)

# 单步产出透给 verifier 的上限：验收不需要读完整篇，但也不能只看摘要。
_OBSERVATION_MAX_CHARS = 4_000


def _strip_fence(raw: str) -> str:
    m = _FENCE_RE.match(raw or "")
    return m.group(1) if m else (raw or "")


def _parse_verdict_json(raw: str, plan: Plan) -> VerifierResult | None:
    """解析模型 verdict；任何不合规返回 None（调用方回退规则版）。"""
    try:
        payload = json.loads(_strip_fence(raw))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    verdict_raw = str(payload.get("verdict") or "").strip().lower()
    verdict = {
        "pass": VerificationVerdict.pass_,
        "revise": VerificationVerdict.revise,
        "fail": VerificationVerdict.fail,
    }.get(verdict_raw)
    if verdict is None:
        return None

    findings_raw = payload.get("findings") or []
    if not isinstance(findings_raw, list):
        return None
    findings = [str(f) for f in findings_raw]

    revise_raw = payload.get("revise_step_ids") or []
    if not isinstance(revise_raw, list):
        return None
    revise_ids = [str(s) for s in revise_raw]

    known = set(plan.step_ids)
    if any(sid not in known for sid in revise_ids):
        # 未知 id 会让 planner 空转甚至抛错 —— 宁可回退也不用。
        return None
    if verdict == VerificationVerdict.revise and not revise_ids:
        # revise 但不指明步骤，planner 无从下手。
        return None

    return VerifierResult(
        verdict=verdict, findings=findings, revise_step_ids=revise_ids
    )


class LLMVerifier:
    """用模型验收；不可用时回退 :class:`RuleBasedVerifier`。"""

    def __init__(
        self,
        *,
        provider: Any,
        model_config: Any = None,
        guard: Any = None,
        fallback: Any = None,
    ) -> None:
        self._provider = provider
        self._model_config = model_config
        self._guard = guard
        self._fallback = fallback or RuleBasedVerifier()

    async def verify(
        self, plan: Plan, observations: dict[str, StepObservation]
    ) -> VerifierResult:
        # 没有可验收的观测 → 直接交给规则版（它会判 revise），不浪费一次调用。
        if not observations:
            return await self._fallback.verify(plan, observations)
        if self._provider is None:
            return await self._fallback.verify(plan, observations)

        from app.agents.policies import BudgetExceeded
        from app.core.config import get_settings
        from app.observability import observe_counter, observe_span

        if self._guard is not None:
            try:
                self._guard.check()
            except BudgetExceeded:
                observe_counter("agent.llm_verifier", 1, outcome="budget_exhausted")
                return await self._fallback.verify(plan, observations)

        timeout_s = float(
            getattr(get_settings(), "AGENT_LLM_VERIFIER_TIMEOUT_S", 10.0)
        )
        from app.providers.base import ChatOptions

        try:
            with observe_span("agent.llm_verify", profile=plan.profile or ""):
                async with asyncio.timeout(timeout_s):
                    result = await self._provider.chat(
                        [
                            {"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": self._render(plan, observations)},
                        ],
                        ChatOptions(temperature=0.0, max_tokens=800),
                    )
        except TimeoutError:
            observe_counter("agent.llm_verifier", 1, outcome="timeout")
            logger.info("LLM verifier timed out; using rule-based verifier")
            return await self._fallback.verify(plan, observations)
        except asyncio.CancelledError:
            raise
        except Exception:
            observe_counter("agent.llm_verifier", 1, outcome="error")
            logger.warning(
                "LLM verifier call failed; using rule-based verifier", exc_info=True
            )
            return await self._fallback.verify(plan, observations)

        from app.agents.workflow.llm_planner import _charge

        _charge(
            self._guard, getattr(result, "usage", None), self._model_config, "verifier"
        )

        parsed = _parse_verdict_json(getattr(result, "content", "") or "", plan)
        if parsed is None:
            observe_counter("agent.llm_verifier", 1, outcome="invalid_verdict")
            logger.info("LLM verifier produced an invalid verdict; using rule-based")
            return await self._fallback.verify(plan, observations)

        observe_counter("agent.llm_verifier", 1, outcome="ok")
        return parsed

    @staticmethod
    def _render(plan: Plan, observations: dict[str, StepObservation]) -> str:
        parts = [f"Goal: {plan.goal}", f"Profile: {plan.profile}", "Steps:"]
        for step in plan.steps:
            obs = observations.get(step.id)
            body = (obs.output if obs else "") or ""
            clipped = body[:_OBSERVATION_MAX_CHARS]
            truncated = " [truncated]" if len(body) > _OBSERVATION_MAX_CHARS else ""
            crit = step.acceptance_criteria or {}
            parts.append(
                f"- id={step.id} name={step.name or step.id}\n"
                f"  asked: {step.task_description}\n"
                f"  criteria: {json.dumps(crit, ensure_ascii=False)}\n"
                f"  output{truncated}: {clipped}"
            )
        return "\n".join(parts)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_llm_verifier.py -q --tb=short
```
Expected: 14 passed

- [ ] **Step 5: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/workflow/llm_verifier.py backend/tests/test_llm_verifier.py
git commit -m "feat(workflow): LLM verifier（非法输出回退规则版）"
```

---

## Task 12: 新拓扑「任务分解」（task_decomposition）

**背景**：现有 3 个 profile 全是「检索→写作」或「辩论」。本任务加一个真正做事的拓扑：协调者拆解 → N 个 worker **并行**执行 → 整合者汇合。这与既有 `parallel_research` 的区别是 worker 数量**动态**（由 plan 的步数决定），且整合者是全量 join。

**Files:**
- Create: `backend/app/agents/crews/task_decomposition.py`
- Modify: `backend/app/agents/crews/__init__.py`
- Modify: `backend/app/agents/graph.py`（静态 builder）
- Modify: `backend/app/agents/workflow/planner.py`（模板）
- Test: `backend/tests/test_task_decomposition.py`（新建）

**Interfaces:**
- Consumes: `StageSpec`（`app.agents.crews.stage`）、`AgentGraph`/`AgentGraphNode`/`AgentGraphEdge`/`EdgeType`/`GraphMode`（`app.agents.graph`）、`Plan`/`Step`（`app.agents.workflow.schemas`）
- Produces:
  - `build_task_decomposition_stages(*, llm, tools, question, worker_count=3) -> tuple[AgentGraph, list[StageSpec]]`
  - `build_task_decomposition_graph(question: str, worker_count: int = 3) -> AgentGraph`
  - `build_task_decomposition_plan(question: str, worker_count: int = 3) -> Plan`
  - `WORKER_PREFIX = "worker-"`（模块常量，供 `graph_from_plan` 的展示层复用判定）

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_task_decomposition.py`：

```python
"""任务分解拓扑：decomposer → worker×N（并行）→ integrator（全量 join）。"""
from __future__ import annotations

from app.agents.graph import (
    build_task_decomposition_graph,
    graph_from_plan,
)
from app.agents.workflow.planner import build_task_decomposition_plan

_Q = "把竞品调研拆成可并行执行的工作项"


def test_plan_shape():
    plan = build_task_decomposition_plan(_Q, worker_count=3)
    ids = plan.step_ids
    assert ids[0] == "decomposer"
    assert ids[-1] == "integrator"
    assert ids[1:4] == ["worker-1", "worker-2", "worker-3"]

    # 所有 worker 只依赖 decomposer → 引擎的 ready 集是 3（真并行）。
    for wid in ("worker-1", "worker-2", "worker-3"):
        assert plan.get(wid).dependencies == ["decomposer"]
    # integrator 是 join：依赖全部 worker。
    assert plan.get("integrator").dependencies == [
        "worker-1", "worker-2", "worker-3"
    ]
    plan.validate()


def test_plan_respects_worker_count():
    plan = build_task_decomposition_plan(_Q, worker_count=5)
    assert len([s for s in plan.step_ids if s.startswith("worker-")]) == 5
    assert plan.get("integrator").dependencies == [f"worker-{i}" for i in range(1, 6)]


def test_graph_topology_matches_plan():
    plan = build_task_decomposition_plan(_Q, worker_count=3)
    got = graph_from_plan(plan)
    want = build_task_decomposition_graph(_Q, worker_count=3)

    assert [n.id for n in got.nodes] == [n.id for n in want.nodes]
    assert [n.stage for n in got.nodes] == [n.stage for n in want.nodes]
    assert [n.lane for n in got.nodes] == [n.lane for n in want.nodes]
    assert {(e.source, e.target) for e in got.edges} == {
        (e.source, e.target) for e in want.edges
    }


def test_graph_presentation_is_chinese_product_copy():
    graph = build_task_decomposition_graph(_Q, worker_count=2)
    decomposer = graph.node("decomposer")
    assert decomposer is not None
    assert decomposer.name  # 展示名非空
    # 面向用户的是中文产品文案，不是 plan 模板里的英文技术串。
    assert any("一" <= ch <= "鿿" for ch in decomposer.role)


def test_workers_share_one_stage_for_parallel_rendering():
    graph = build_task_decomposition_graph(_Q, worker_count=3)
    stages = {n.id: n.stage for n in graph.nodes}
    assert stages["decomposer"] == 0
    assert stages["worker-1"] == stages["worker-2"] == stages["worker-3"] == 1
    assert stages["integrator"] == 2
    # 同 stage 内的 lane 各不相同（面板并排渲染依赖此）。
    lanes = {n.id: n.lane for n in graph.nodes if n.stage == 1}
    assert sorted(lanes.values()) == [0, 1, 2]


def test_worker_count_is_clamped():
    """防止调用方传入 0 或负数导致空 worker 集。"""
    plan = build_task_decomposition_plan(_Q, worker_count=0)
    assert any(s.startswith("worker-") for s in plan.step_ids)
    plan2 = build_task_decomposition_plan(_Q, worker_count=-3)
    assert any(s.startswith("worker-") for s in plan2.step_ids)
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_task_decomposition.py -q --tb=short
```
Expected: FAIL — `ImportError: cannot import name 'build_task_decomposition_graph'`

- [ ] **Step 3: 加静态 builder（`graph.py`）**

在 `build_graph_for_profile` 之前插入：

```python
_WORKER_COUNT_MIN = 1
_WORKER_COUNT_MAX = 6


def _clamp_workers(n: int) -> int:
    """把 worker 数量夹到合理区间。

    上限存在的理由：worker 数与并行度、token 消耗、面板宽度都成正比；
    一个模型给出的超大 plan 不该把运行拖垮。
    """
    try:
        v = int(n)
    except (TypeError, ValueError):
        v = 3
    return max(_WORKER_COUNT_MIN, min(_WORKER_COUNT_MAX, v))


def build_task_decomposition_graph(
    question: str, worker_count: int = 3
) -> AgentGraph:
    """Coordinator → Worker ×N（并行） → Integrator（全量 join）。

    与 parallel_research 的区别：worker 数量动态，且整合者是**全量** join
    （parallel_research 的 analyst 只汇合两条固定线）。
    """
    n = _clamp_workers(worker_count)
    nodes = [
        AgentGraphNode(
            id="decomposer", name="Coordinator", role="任务拆解",
            task_title="把任务拆成可并行的工作项",
            task_summary="理解目标，拆出彼此独立、可同时推进的工作项",
            stage=0, lane=0,
        )
    ]
    edges = []
    for i in range(1, n + 1):
        wid = f"worker-{i}"
        nodes.append(
            AgentGraphNode(
                id=wid, name=f"Worker {i}", role="执行",
                task_title=f"执行工作项 {i}",
                task_summary="独立完成分配到的子任务，产出可直接汇合的结果",
                stage=1, lane=i - 1,
            )
        )
        edges.append(
            AgentGraphEdge(
                id=f"decomposer-{wid}", source="decomposer", target=wid,
                type=EdgeType.dependency, label="分发工作项",
            )
        )
    nodes.append(
        AgentGraphNode(
            id="integrator", name="Integrator", role="结果整合",
            task_title="汇合并整合全部工作项",
            task_summary="汇总所有 worker 的产出，消除冲突，形成统一交付物",
            stage=2, lane=0,
        )
    )
    for i in range(1, n + 1):
        edges.append(
            AgentGraphEdge(
                id=f"worker-{i}-integrator",
                source=f"worker-{i}", target="integrator",
                type=EdgeType.handoff, label=f"移交工作项 {i} 的产出",
            )
        )
    return AgentGraph(
        run_id="",
        runtime="crewai",
        flow_name="task_decomposition",
        mode=GraphMode.parallel,
        status="pending",
        nodes=nodes,
        edges=edges,
    )
```

- [ ] **Step 4: 让 `build_graph_for_profile` 认识新 profile**

把 `build_graph_for_profile` 替换为：

```python
def build_graph_for_profile(profile: str, question: str) -> AgentGraph:
    """Pick the topology by agent_profile / intent."""
    if profile == "parallel_research":
        return build_parallel_research_graph(question)
    if profile == "task_decomposition":
        return build_task_decomposition_graph(question)
    if profile == "write_review":
        return build_write_review_graph(question)
    if profile == "debate":
        from app.agents.planning import extract_debate_sides

        sides = extract_debate_sides(question)
        return build_debate_graph(
            sides.side_a if sides else "A", sides.side_b if sides else "B"
        )
    return build_deep_research_graph(question)
```

（`build_write_review_graph` 在 Task 13 实现；本任务是先把 `task_decomposition` 分支加上，若 Task 13 尚未做则暂时只加这一个分支，Task 13 再补另一个。）

- [ ] **Step 5: 加 plan 模板（`planner.py`）**

在 `build_debate_plan` 之后插入：

```python
def build_task_decomposition_plan(question: str, worker_count: int = 3) -> Plan:
    """Coordinator → Worker ×N（并行） → Integrator（全量 join）。

    worker 之间无依赖，所以 decomposer 完成后它们同时在 ready 集里，
    引擎会真并行执行（max_concurrency == N）。
    """
    from app.agents.graph import _clamp_workers

    q = (question or "").strip()
    n = _clamp_workers(worker_count)
    steps = [
        Step(
            id="decomposer",
            role="coordinator",
            name="Coordinator",
            task_description=(
                f"Break the request into {n} independent, parallelisable work "
                f"items: {q}"
            ),
            dependencies=[],
            acceptance_criteria={"min_chars": 1},
        )
    ]
    for i in range(1, n + 1):
        steps.append(
            Step(
                id=f"worker-{i}",
                role="worker",
                name=f"Worker {i}",
                task_description=(
                    f"Complete work item {i} from the coordinator's breakdown. "
                    "Produce a self-contained result that needs no further work."
                ),
                dependencies=["decomposer"],
                retry_policy=_TRANSIENT,
                acceptance_criteria={"min_chars": 1},
            )
        )
    steps.append(
        Step(
            id="integrator",
            role="integrator",
            name="Integrator",
            task_description=(
                "Merge every worker's output into one coherent deliverable; "
                "resolve conflicts and remove duplication."
            ),
            dependencies=[f"worker-{i}" for i in range(1, n + 1)],
            acceptance_criteria={"min_chars": 1},
        )
    )
    return Plan(
        version=1, goal=q, profile="task_decomposition",
        steps=steps, max_replans=1,
    )
```

在 `build_plan_for_profile` 中加入分支：

```python
    if profile == "task_decomposition":
        return build_task_decomposition_plan(question)
```

- [ ] **Step 6: 加 stage builder（`crews/task_decomposition.py`）**

创建 `backend/app/agents/crews/task_decomposition.py`：

```python
"""任务分解拓扑：Coordinator → Worker ×N（并行） → Integrator。

这是唯一一个 worker 数量**动态**的 profile：协调者拆出 N 个工作项，N 个
worker 无依赖地并行执行，整合者全量汇合。

三个角色的 backstory 都把「不要越界」写进规则 —— 多 Agent 最常见的失败是
worker 互相重复、整合者重新做一遍而不是汇合。
"""
from __future__ import annotations

from typing import Any

from app.agents.crews.stage import StageSpec
from app.agents.graph import AgentGraph, _clamp_workers, build_task_decomposition_graph

_DECOMPOSER_BACKSTORY = (
    "You are a task coordinator. Rules:\n"
    "1) Break the user's request into {n} work items that are genuinely "
    "INDEPENDENT — no item should need another's result.\n"
    "2) Each item must be concrete enough that a worker can complete it "
    "without asking follow-up questions.\n"
    "3) Do NOT do the work yourself and do NOT produce the final answer.\n"
    "4) Output a numbered list; item i will be handed to Worker i.\n"
    "5) Answer in the user's language."
)

_WORKER_BACKSTORY = (
    "You are worker {i} of {n} independent workers. Rules:\n"
    "1) Complete ONLY the work item assigned to you — do not attempt other "
    "items, and do not write the final deliverable.\n"
    "2) Your output must be SELF-CONTAINED: the integrator sees only your "
    "result, not your reasoning.\n"
    "3) Do not fabricate unverifiable facts. Where unsure, say so.\n"
    "4) Answer in the user's language."
)

_INTEGRATOR_BACKSTORY = (
    "You are the integrator. Every worker's output is in your context. Rules:\n"
    "1) MERGE the workers' outputs — do not redo their work from scratch.\n"
    "2) Resolve conflicts explicitly: say which version you kept and why.\n"
    "3) Remove duplication; the result must read as ONE deliverable.\n"
    "4) If a worker's output is missing or clearly unusable, say so rather "
    "than silently papering over it.\n"
    "5) Answer in the user's language, in well-structured Markdown."
)


def build_task_decomposition_stages(
    *, llm: Any, tools: list[Any], question: str, worker_count: int = 3
) -> tuple[AgentGraph, list[StageSpec]]:
    """Build the decomposition flow. ``tools`` is unused (matches the signature
    of the other crew builders so the runtime can dispatch uniformly)."""
    from crewai import Agent, Task

    n = _clamp_workers(worker_count)

    decomposer = Agent(
        role="Coordinator",
        goal=f"Split the request into {n} independent work items.",
        backstory=_DECOMPOSER_BACKSTORY.format(n=n),
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )
    integrator = Agent(
        role="Integrator",
        goal="Merge every worker's output into one coherent deliverable.",
        backstory=_INTEGRATOR_BACKSTORY,
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )

    stages: list[StageSpec] = [
        StageSpec(
            agent_id="decomposer",
            agent=decomposer,
            task=Task(
                description=(
                    f"User request: {question}\n\n"
                    f"Break it into {n} independent work items. Output a "
                    "numbered list."
                ),
                expected_output="A numbered list of independent work items.",
                agent=decomposer,
            ),
            depends_on=[],
            stage=0,
        )
    ]

    worker_ids: list[str] = []
    for i in range(1, n + 1):
        wid = f"worker-{i}"
        worker_ids.append(wid)
        worker = Agent(
            role=f"Worker {i}",
            goal=f"Complete work item {i} from the coordinator's breakdown.",
            backstory=_WORKER_BACKSTORY.format(i=i, n=n),
            llm=llm,
            allow_delegation=False,
            verbose=False,
        )
        stages.append(
            StageSpec(
                agent_id=wid,
                agent=worker,
                task=Task(
                    description=(
                        f"User request: {question}\n\n"
                        f"You are Worker {i}. The coordinator split the request "
                        "into numbered items (in your context). Complete item "
                        f"{i} only, and produce a self-contained result."
                    ),
                    expected_output="A self-contained result for this work item.",
                    agent=worker,
                ),
                depends_on=["decomposer"],
                stage=1,
            )
        )

    stages.append(
        StageSpec(
            agent_id="integrator",
            agent=integrator,
            task=Task(
                description=(
                    f"User request: {question}\n\n"
                    "Every worker's result is in your context. Merge them into "
                    "one coherent deliverable, resolving conflicts explicitly."
                ),
                expected_output="One merged, coherent Markdown deliverable.",
                agent=integrator,
            ),
            depends_on=worker_ids,
            stage=2,
        )
    )

    graph = build_task_decomposition_graph(question, worker_count=n)
    return graph, stages
```

- [ ] **Step 7: 导出**

在 `crews/__init__.py` 里加 import 与 `__all__` 条目：

```python
from app.agents.crews.task_decomposition import build_task_decomposition_stages
```
```python
    "build_task_decomposition_stages",
```

- [ ] **Step 8: 回填引擎的 builder 字典**

Task 5 Step 9 引入了 `_build_stage_adapter` 的 `builders` 字典。把本任务的拓扑加进去：

```python
            "task_decomposition": build_task_decomposition_stages,
```

并在该文件的 import 里加上 `build_task_decomposition_stages`。

- [ ] **Step 9: 运行测试确认通过**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_task_decomposition.py -q --tb=short
```
Expected: 6 passed

- [ ] **Step 10: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/crews/task_decomposition.py backend/app/agents/crews/__init__.py backend/app/agents/graph.py backend/app/agents/workflow/planner.py backend/tests/test_task_decomposition.py
git commit -m "feat(agents): 任务分解拓扑（动态 worker 数 + 全量 join）"
```

---

## Task 13: 新拓扑「写-审-改」（write_review）

**背景**：与 `deep_research` 的区别是**质量由第二次触碰保证**——审阅者拿结构化问题清单，定稿者据其修改。审阅者被明确禁止自己写终稿（否则三跳退化成两跳）。

**Files:**
- Create: `backend/app/agents/crews/write_review.py`
- Modify: `backend/app/agents/crews/__init__.py`
- Modify: `backend/app/agents/graph.py`、`backend/app/agents/workflow/planner.py`
- Test: `backend/tests/test_write_review.py`（新建）

**Interfaces:**
- Produces:
  - `build_write_review_stages(*, llm, tools, question) -> tuple[AgentGraph, list[StageSpec]]`
  - `build_write_review_graph(question: str) -> AgentGraph`
  - `build_write_review_plan(question: str) -> Plan`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_write_review.py`：

```python
"""写-审-改拓扑：drafter → reviewer → finalizer（严格串行）。"""
from __future__ import annotations

from app.agents.graph import build_write_review_graph, graph_from_plan
from app.agents.workflow.planner import build_write_review_plan

_Q = "写一篇发布说明，说明这次 Agent 能力升级"


def test_plan_is_strictly_sequential():
    plan = build_write_review_plan(_Q)
    assert plan.step_ids == ["drafter", "reviewer", "finalizer"]
    assert plan.get("drafter").dependencies == []
    assert plan.get("reviewer").dependencies == ["drafter"]
    assert plan.get("finalizer").dependencies == ["reviewer"]
    plan.validate()


def test_graph_topology_matches_plan():
    plan = build_write_review_plan(_Q)
    got = graph_from_plan(plan)
    want = build_write_review_graph(_Q)
    assert [n.id for n in got.nodes] == [n.id for n in want.nodes]
    assert [n.stage for n in got.nodes] == [n.stage for n in want.nodes]
    assert {(e.source, e.target) for e in got.edges} == {
        (e.source, e.target) for e in want.edges
    }


def test_graph_uses_chinese_product_copy():
    graph = build_write_review_graph(_Q)
    for node in graph.nodes:
        assert node.role, f"{node.id} 缺 role"
        assert any("一" <= ch <= "鿿" for ch in node.role), (
            f"{node.id} 的 role 应当是中文产品文案"
        )


def test_stages_are_sequential():
    graph = build_write_review_graph(_Q)
    assert {n.id: n.stage for n in graph.nodes} == {
        "drafter": 0, "reviewer": 1, "finalizer": 2
    }
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_write_review.py -q --tb=short
```
Expected: FAIL — `ImportError: cannot import name 'build_write_review_graph'`

- [ ] **Step 3: 静态 builder**

在 `graph.py` 的 `build_single_agent_graph` 之前插入：

```python
def build_write_review_graph(question: str) -> AgentGraph:
    """Draft → Review → Finalize（严格串行）。

    与 deep_research 的区别：这里不检索，质量靠**第二次触碰**保证 ——
    审阅者给出结构化问题清单，定稿者据其修改。
    """
    return AgentGraph(
        run_id="",
        runtime="crewai",
        flow_name="write_review",
        mode=GraphMode.sequential,
        status="pending",
        nodes=[
            AgentGraphNode(
                id="drafter", name="Drafter", role="初稿撰写",
                task_title="产出可审阅的初稿",
                task_summary="按需求写出结构完整的初稿，交付给审阅者",
                stage=0, lane=0,
            ),
            AgentGraphNode(
                id="reviewer", name="Reviewer", role="质量审阅",
                task_title="找出必须修改的问题",
                task_summary="逐项检查事实、逻辑、结构与表达，输出结构化问题清单",
                stage=1, lane=0,
            ),
            AgentGraphNode(
                id="finalizer", name="Finalizer", role="定稿",
                task_title="按审阅意见定稿",
                task_summary="逐条落实审阅意见，产出可直接交付的最终版本",
                stage=2, lane=0,
            ),
        ],
        edges=[
            AgentGraphEdge(id="drafter-reviewer", source="drafter", target="reviewer",
                           type=EdgeType.handoff, label="移交初稿"),
            AgentGraphEdge(id="reviewer-finalizer", source="reviewer", target="finalizer",
                           type=EdgeType.handoff, label="移交问题清单"),
        ],
    )
```

- [ ] **Step 4: plan 模板**

在 `planner.py` 的 `build_task_decomposition_plan` 之后插入：

```python
def build_write_review_plan(question: str) -> Plan:
    """Draft → Review → Finalize（严格串行）。"""
    q = (question or "").strip()
    return Plan(
        version=1, goal=q, profile="write_review",
        max_replans=1,
        steps=[
            Step(
                id="drafter", role="drafter", name="Drafter",
                task_description=f"Write a complete first draft for: {q}",
                dependencies=[],
                acceptance_criteria={"min_chars": 1},
            ),
            Step(
                id="reviewer", role="reviewer", name="Reviewer",
                task_description=(
                    "Review the draft against the request. Output a STRUCTURED "
                    "list of concrete problems (factual, logical, structural, "
                    "clarity). Do NOT rewrite the draft yourself."
                ),
                dependencies=["drafter"],
                acceptance_criteria={"min_chars": 1},
            ),
            Step(
                id="finalizer", role="finalizer", name="Finalizer",
                task_description=(
                    "Produce the final version, addressing every point in the "
                    "reviewer's list. Keep what was already good."
                ),
                dependencies=["reviewer"],
                acceptance_criteria={"min_chars": 1},
            ),
        ],
    )
```

并在 `build_plan_for_profile` 里加分支：

```python
    if profile == "write_review":
        return build_write_review_plan(question)
```

- [ ] **Step 5: crew builder**

创建 `backend/app/agents/crews/write_review.py`：

```python
"""写-审-改拓扑：Drafter → Reviewer → Finalizer（严格串行）。

质量保障来自**第二次触碰**：审阅者被明确禁止自己写终稿，只输出结构化问题
清单；定稿者据其逐条修改。若审阅者直接给"改好的版本"，这个拓扑就退化成
一次串行改写，三跳的价值全部蒸发。
"""
from __future__ import annotations

from typing import Any

from app.agents.crews.stage import StageSpec
from app.agents.graph import AgentGraph, build_write_review_graph

_DRAFTER_BACKSTORY = (
    "You are a drafting specialist. Rules:\n"
    "1) Produce a COMPLETE first draft that could ship as-is if nobody "
    "reviewed it — do not leave placeholders or TODO notes.\n"
    "2) Structure it clearly: it will be reviewed section by section.\n"
    "3) Do not fabricate unverifiable facts.\n"
    "4) Answer in the user's language."
)

_REVIEWER_BACKSTORY = (
    "You are a strict reviewer. Rules:\n"
    "1) Output a STRUCTURED list of concrete problems, grouped by kind: "
    "factual errors, logical gaps, structural problems, unclear passages.\n"
    "2) Each item must say WHAT is wrong and WHY it matters.\n"
    "3) Do NOT rewrite the draft and do NOT produce a corrected version — "
    "your job is diagnosis, not treatment.\n"
    "4) If a section is genuinely fine, say so rather than inventing problems.\n"
    "5) Answer in the user's language."
)

_FINALIZER_BACKSTORY = (
    "You are the finalizer. The draft and the reviewer's problem list are both "
    "in your context. Rules:\n"
    "1) Address EVERY item in the reviewer's list. If you disagree with one, "
    "say so explicitly and keep your version.\n"
    "2) Preserve what was already good — do not rewrite for the sake of it.\n"
    "3) The result must be a finished deliverable, not a revision memo.\n"
    "4) Answer in the user's language."
)


def build_write_review_stages(
    *, llm: Any, tools: list[Any], question: str
) -> tuple[AgentGraph, list[StageSpec]]:
    """Build the write-review flow. ``tools`` is unused (signature parity)."""
    from crewai import Agent, Task

    drafter = Agent(
        role="Drafter",
        goal="Produce a complete, reviewable first draft.",
        backstory=_DRAFTER_BACKSTORY,
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )
    reviewer = Agent(
        role="Reviewer",
        goal="Diagnose concrete problems in the draft; do not rewrite it.",
        backstory=_REVIEWER_BACKSTORY,
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )
    finalizer = Agent(
        role="Finalizer",
        goal="Produce the final version, addressing every review point.",
        backstory=_FINALIZER_BACKSTORY,
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )

    graph = build_write_review_graph(question)
    stages = [
        StageSpec(
            agent_id="drafter",
            agent=drafter,
            task=Task(
                description=(
                    f"User request: {question}\n\n"
                    "Produce a complete first draft. It will be reviewed next."
                ),
                expected_output="A complete, structured first draft.",
                agent=drafter,
            ),
            depends_on=[],
            stage=0,
        ),
        StageSpec(
            agent_id="reviewer",
            agent=reviewer,
            task=Task(
                description=(
                    f"User request: {question}\n\n"
                    "The draft is in your context. Output a structured list of "
                    "concrete problems. Do NOT rewrite it."
                ),
                expected_output=(
                    "A structured problem list: factual / logical / structural "
                    "/ clarity, each with what and why."
                ),
                agent=reviewer,
            ),
            depends_on=["drafter"],
            stage=1,
        ),
        StageSpec(
            agent_id="finalizer",
            agent=finalizer,
            task=Task(
                description=(
                    f"User request: {question}\n\n"
                    "The draft and the reviewer's problem list are in context. "
                    "Produce the finished deliverable, addressing every point."
                ),
                expected_output="The finished deliverable in Markdown.",
                agent=finalizer,
            ),
            depends_on=["reviewer"],
            stage=2,
        ),
    ]
    return graph, stages
```

- [ ] **Step 6: 导出**

在 `crews/__init__.py` 加：

```python
from app.agents.crews.write_review import build_write_review_stages
```
```python
    "build_write_review_stages",
```

- [ ] **Step 7: 回填引擎的 builder 字典**

同 Task 12：把 `"write_review": build_write_review_stages` 加进 `_build_stage_adapter` 的 `builders` 字典与对应 import。

- [ ] **Step 8: 运行测试确认通过**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_write_review.py tests/test_task_decomposition.py -q --tb=short
```
Expected: 全绿

- [ ] **Step 9: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/crews/write_review.py backend/app/agents/crews/__init__.py backend/app/agents/graph.py backend/app/agents/workflow/planner.py backend/tests/test_write_review.py
git commit -m "feat(agents): 写-审-改拓扑（审阅者只诊断不改写）"
```

---

## Task 14: 引擎路径接入 LLM 规划器与 verifier

**Files:**
- Modify: `backend/app/agents/orchestrator.py`
- Test: `backend/tests/test_engine_llm_wiring.py`（新建）

**Interfaces:**
- Consumes: Task 10/11 的 `build_plan_with_llm` / `LLMVerifier`；Task 5 的名单路由
- Produces: 引擎路径按 flag 选择 planner 与 verifier

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_engine_llm_wiring.py`：

```python
"""引擎路径按 flag 选择 planner / verifier。

默认关：不开 flag 时行为与现在完全一致（模板 plan + 规则 verifier）。
"""
from __future__ import annotations

import pytest

from app.agents.orchestrator import ChatOrchestrator
from app.agents.workflow.llm_planner import build_plan_with_llm
from app.agents.workflow.llm_verifier import LLMVerifier
from app.agents.workflow.verifier import RuleBasedVerifier
from app.core.config import get_settings


def test_llm_planner_flag_default_off():
    assert get_settings().AGENT_LLM_PLANNER is False


def test_llm_verifier_flag_default_off():
    assert get_settings().AGENT_LLM_VERIFIER is False


def test_engine_builds_rule_verifier_by_default():
    """不开 flag 时必须用规则版 —— 这是已知行为。"""
    o = ChatOrchestrator()
    verifier = o._engine_verifier(provider=None, model_config=None, guard=None)
    assert isinstance(verifier, RuleBasedVerifier)


def test_engine_builds_llm_verifier_when_enabled(monkeypatch):
    monkeypatch.setattr(
        get_settings(), "AGENT_LLM_VERIFIER", True, raising=False
    )
    o = ChatOrchestrator()
    verifier = o._engine_verifier(
        provider=object(), model_config=None, guard=None
    )
    assert isinstance(verifier, LLMVerifier)


def test_engine_falls_back_to_rule_verifier_without_provider(monkeypatch):
    """flag 开着但拿不到 provider 时，必须回退规则版而不是崩。"""
    monkeypatch.setattr(
        get_settings(), "AGENT_LLM_VERIFIER", True, raising=False
    )
    o = ChatOrchestrator()
    verifier = o._engine_verifier(provider=None, model_config=None, guard=None)
    assert isinstance(verifier, RuleBasedVerifier)
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_engine_llm_wiring.py -q --tb=short
```
Expected: FAIL — `AttributeError: 'ChatOrchestrator' object has no attribute '_engine_verifier'`

- [ ] **Step 3: 实现 verifier 选择器**

在 `orchestrator.py` 的 `_build_stage_adapter` 之前插入：

```python
    def _engine_verifier(self, *, provider: Any, model_config: Any, guard: Any) -> Any:
        """按 flag 选 verifier。默认规则版；flag 开但没 provider 时也回退规则版。"""
        from app.agents.workflow.verifier import RuleBasedVerifier

        if not bool(getattr(get_settings(), "AGENT_LLM_VERIFIER", False)):
            return RuleBasedVerifier()
        if provider is None:
            return RuleBasedVerifier()
        from app.agents.workflow.llm_verifier import LLMVerifier

        return LLMVerifier(
            provider=provider, model_config=model_config, guard=guard
        )
```

- [ ] **Step 4: 接入 planner 与 verifier**

在 `_run_engine_path` 里，把 `plan = build_deep_research_plan(question)` 替换为：

```python
        # LLM 规划器（flag 开时）：永远返回可用 plan（失败即回退模板）。
        if bool(getattr(get_settings(), "AGENT_LLM_PLANNER", False)):
            from app.agents.workflow.llm_planner import build_plan_with_llm

            plan = await build_plan_with_llm(
                provider=env.stage_ctx.provider,
                model_config=ctx.model_config,
                profile="deep_research",
                question=question,
                guard=env.guard,
            )
        else:
            plan = build_deep_research_plan(question)
```

把 `verifier=RuleBasedVerifier(),` 改为：

```python
            verifier=self._engine_verifier(
                provider=env.stage_ctx.provider,
                model_config=ctx.model_config,
                guard=env.guard,
            ),
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_engine_llm_wiring.py tests/test_engine_routing.py -q --tb=short
```
Expected: 全绿

- [ ] **Step 6: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/orchestrator.py backend/tests/test_engine_llm_wiring.py
git commit -m "feat(agents): 引擎路径按 flag 接入 LLM 规划器与 verifier"
```

---

## Task 15: 新 profile 纳入自动路由

**背景**：spec §7.4 —— 自动升级**只认模型给出的显式 route**，不在关键词路径（`decide_route`）加匹配规则。「任务」「审阅」这类常见词若走关键词匹配会频繁误触发。

**Files:**
- Modify: `backend/app/agents/intent_router.py`
- Modify: `backend/app/agents/schemas.py`（`IntentDecision.route` 的合法值）
- Test: `backend/tests/test_new_profile_routing.py`（新建）

**Interfaces:**
- Consumes: `decide_route_with_intent`、`IntentDecision`
- Produces: `decide_route_with_intent` 支持 `route ∈ {task_decomposition, write_review}`

- [ ] **Step 1: 写失败的测试**

创建 `backend/tests/test_new_profile_routing.py`：

```python
"""新 profile 的路由：只认模型显式点名，不靠关键词猜测。"""
from __future__ import annotations

from app.agents.intent_router import decide_route, decide_route_with_intent
from app.agents.schemas import IntentDecision


def _intent(route: str, *, confidence: float = 0.9, kind: str = "document"):
    return IntentDecision(
        route=route, deliverable_kind=kind, confidence=confidence,
        rationale="test", tool_hints=[],
    )


def test_keyword_router_never_picks_new_profiles():
    """关键词路径不得把普通请求误判成新拓扑 —— 这是误触发的源头。"""
    for text in (
        "帮我完成一个任务",
        "审阅一下这段文字",
        "把这个分解成几步",
        "写一份报告然后审阅",
    ):
        d = decide_route("auto", user_content=text)
        assert d.agent_profile not in ("task_decomposition", "write_review"), (
            f"{text!r} 被关键词路径误路由到 {d.agent_profile}"
        )


def test_intent_route_task_decomposition():
    d = decide_route_with_intent(
        "auto", user_content="把调研拆成并行工作项", intent=_intent("task_decomposition")
    )
    assert d.agent_profile == "task_decomposition"
    assert d.use_multi_agent is True
    assert d.execution_mode.value == "agent"


def test_intent_route_write_review():
    d = decide_route_with_intent(
        "auto", user_content="写一份发布说明", intent=_intent("write_review")
    )
    assert d.agent_profile == "write_review"
    assert d.use_multi_agent is True


def test_low_confidence_intent_falls_back_to_keyword_router():
    d = decide_route_with_intent(
        "auto", user_content="把调研拆成并行工作项",
        intent=_intent("task_decomposition", confidence=0.1),
    )
    assert d.agent_profile != "task_decomposition"


def test_code_deliverable_wins_over_new_profile():
    """代码请求仍走 native —— 新拓扑的 writer 会截断代码。"""
    d = decide_route_with_intent(
        "auto", user_content="写一个贪吃蛇游戏",
        intent=_intent("write_review", kind="code"),
    )
    assert d.use_multi_agent is False
    assert d.disable_web is True


def test_speed_mode_never_escalates():
    d = decide_route_with_intent(
        "speed", user_content="把调研拆成并行工作项",
        intent=_intent("task_decomposition"),
    )
    assert d.use_multi_agent is False
    assert d.mode == "speed"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_new_profile_routing.py -q --tb=short
```
Expected: FAIL — 新 profile 未被路由

- [ ] **Step 3: 在 `decide_route_with_intent` 加分支**

在 `route_name in ("deep_research", "parallel_research")` 分支之后插入：

```python
    # 新协作拓扑：只认模型显式点名（关键词路径不产出这两个值，见 decide_route）。
    if route_name in ("task_decomposition", "write_review"):
        return RouteDecision(
            execution_mode=ExecutionMode.agent,
            agent_profile=route_name,
            enable_tools=True,
            use_multi_agent=True,
            mode=mode,
            requested_mode=mode,
        )
```

- [ ] **Step 4: 确认 `IntentDecision` 接受新 route 值**

检查 `schemas.py` 的 `IntentDecision.route`。若是 `Literal[...]`，把两个新值加进去；若是普通 `str`，无需改动。同时在 `intent_service.py` 的分类提示词（若有枚举说明）中补上这两个选项，否则模型永远不会产出它们。

- [ ] **Step 5: 运行测试确认通过**

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/test_new_profile_routing.py tests/test_intent_router.py tests/test_intent_service.py -q --tb=short
```
Expected: 全绿

- [ ] **Step 6: lint + 提交**

```bash
cd backend && ruff check app tests
cd /d/Gitee/MyGPT && git add backend/app/agents/intent_router.py backend/app/agents/schemas.py backend/app/agents/intent_service.py backend/tests/test_new_profile_routing.py
git commit -m "feat(agents): 新拓扑纳入自动路由（仅模型显式点名）"
```

---

## Task 16: 前端新 profile 展示

**Files:**
- Modify: `frontend/src/components/agents/agent-run-header.tsx`
- Modify: `frontend/src/lib/agent-graph-types.ts`（若 flowName 有联合类型）
- Test: `frontend/src/components/__tests__/agent-step-visibility.test.tsx`（追加）

**Interfaces:**
- Produces: `task_decomposition` / `write_review` 的中文标签

- [ ] **Step 1: 写失败的测试**

追加到 `frontend/src/components/__tests__/agent-step-visibility.test.tsx`：

```ts
describe("新 profile 标签", () => {
  it("任务分解与写-审-改都有中文标签", async () => {
    const mod = await import("@/components/agents/agent-run-header");
    const labels = mod.PROFILE_LABELS;
    expect(labels.task_decomposition).toContain("任务分解");
    expect(labels.write_review).toContain("写");
    expect(labels.deep_research).toBeTruthy();
    expect(labels.debate).toBeTruthy();
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd frontend && npx vitest run src/components/__tests__/agent-step-visibility.test.tsx
```
Expected: FAIL — `PROFILE_LABELS` 未导出或不含新值

- [ ] **Step 3: 导出标签映射并补齐新 profile**

在 `agent-run-header.tsx` 中把内联的 profile 标签对象提取为可导出常量（若已是导出的则直接补值）：

```ts
/** profile → 面向用户的中文标签。导出以便单测直接断言，不必渲染。 */
export const PROFILE_LABELS: Record<string, string> = {
  deep_research: "深度研究",
  parallel_research: "并行研究",
  debate: "辩论",
  task_decomposition: "任务分解",
  write_review: "写-审-改",
  single_agent: "单 Agent",
};
```

并用它替换组件内的内联映射（保持一致，避免两处漂移）。

- [ ] **Step 4: 运行测试确认通过**

```bash
cd frontend && npx vitest run src/components/__tests__/agent-step-visibility.test.tsx
```
Expected: 全绿

- [ ] **Step 5: 全量前端门禁**

```bash
cd frontend && npm run typecheck && npm run lint && npm run test
```
Expected: 全绿

- [ ] **Step 6: 提交**

```bash
cd /d/Gitee/MyGPT && git add frontend/src/components/agents/agent-run-header.tsx frontend/src/components/__tests__/agent-step-visibility.test.tsx
git commit -m "feat(frontend): 新 profile 中文标签"
```

---

## Task 17: 收尾验证

**Files:** 无代码改动

- [ ] **Step 1: 后端全量**

```bash
cd backend && ruff check app tests && .venv/Scripts/python.exe -m pytest tests -q --tb=short \
  --deselect tests/test_agent_phase2.py::test_agent_mode_emits_plan_created \
  --deselect tests/test_agent_phase5.py::test_full_native_agent_path \
  --deselect tests/test_durable_controls.py::test_multi_agent_approval_pauses_then_resumes
```
Expected: **≥ 1226 passed**（基线 + 本计划新增），零失败

- [ ] **Step 2: 前端全量**

```bash
cd frontend && npm run typecheck && npm run lint && npm run test
```
Expected: **≥ 251 passed**，零失败

- [ ] **Step 3: 迁移检查**

```bash
cd /d/Gitee/MyGPT && git diff --name-only HEAD~17 HEAD | grep "migrations/versions" || echo "零迁移 ✓"
```
若**意外**出现迁移文件，推 main 前必须跑 `./scripts/verify_migrations.sh`。

- [ ] **Step 4: 端到端人工验证（关键）**

启动后端 + 前端（`./start.bat`），确认：

1. **重试修复**：把一个模型的 `api_base_url` 指向一个会拒连的端口，发一条 `expert` 模式消息。节点应显示「重试中」而非「已完成+错误」。
2. **计划门**：`expert` 轮次应立即开始执行（不阻塞），面板显示计划且可修改。
3. **辩论入口**：模式选择器出现「辩论模式」，选中后跑出双 advocate + judge。
4. **新拓扑**：以 `task_decomposition` 为 profile 跑一次，确认多 worker 并行、整合者汇合。
5. **可控性**：运行中点「暂停」应真的停住，点「恢复」应继续。

- [ ] **Step 5: 灰度开关自检**

在 `.env` 里设 `AGENT_WORKFLOW_ENGINE=1` + `AGENT_WORKFLOW_ENGINE_PROFILES=""`（空名单），确认**没有任何 profile 走引擎**（安全默认生效）。然后设成 `deep_research` 确认只有它走引擎。

- [ ] **Step 6: 提交（若有修复）**

```bash
cd /d/Gitee/MyGPT && git add -A && git commit -m "test(agents): 收尾验证修复"
```

---

## 收尾检查

- [ ] 后端全量 ≥ 1226 passed，`ruff check` 全绿
- [ ] 前端全量 ≥ 251 passed，typecheck/lint 干净
- [ ] 零迁移（或已跑 `verify_migrations.sh`）
- [ ] 所有新 flag 默认值为安全值（见 Global Constraints 的表）
- [ ] 人工验证 5 项全过
- [ ] `git status -sb` 显示 `ahead N`；**推送与否由用户决定**

## 已知延后项（不在本计划范围）

1. **verifier 的自动修正**：`revise` 目前只触发重跑与整合，不改写他人产出。让 verifier 直接给修正版是更大的设计（涉及产出所有权），留给后续。
2. **`task_decomposition` 的 worker 数由 LLM 规划器决定**：当前模板默认 3，LLM 规划器开启时会产出各自的 worker 数（因为 plan 由模型生成）。两者的一致性测试在 Task 12 覆盖了模板路径。
3. **非 writer stage 的逐 token 流式**：子项目 1 §7.1 的论证不变。
