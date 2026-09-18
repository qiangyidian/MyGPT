# 多 Agent 运行环境统一 + 过程可见性（子项目 1）

- 日期：2026-09-18
- 状态：已批准（Approved）
- 范围：backend（主）+ frontend
- 上游：本设计是「多 Agent 能力强化」三子项目的第 1 个。后续：子项目 2（profile 迁移到引擎 + 计划审批门 + 辩论入口）、子项目 3（LLM 规划器 / LLM verifier / 新拓扑）。

## 1. 背景与问题

### 1.1 现状结构

多 Agent 有**两套执行栈**，功能重叠但成熟度悬殊：

| 能力 | `CrewAIRuntime._walk_stages`（在跑） | `orchestrator._run_engine_path`（沉睡） |
|---|---|---|
| 拓扑来源 | 硬编码 `build_*_stages` / `build_*_graph` | `build_deep_research_plan` 模板 |
| 调度 | 按 `StageSpec.stage` 整数分组 + 同组 `asyncio.gather` | 按 `Step.dependencies` 的 DAG + 有界并发 |
| 重试 | 无（fail-fast） | `RetryPolicy` 分级（transient / permanent） |
| 验证 / 重规划 | 无 | `Verifier` → `replan` 带已完成工作结转 |
| attempt 落库 | 无 | `AgentAttempt` + `step.*` 事件 |
| 工具审批桥 | ✅ `ApprovalBridge` | ❌ |
| writer 流式 | ✅ `StreamingWriterExecutor` | ❌ 仅末尾一次性 `ev_token` |
| 暂停 / 恢复 / 追加指令 | ✅ `_respect_controls` + 持久命令 drain | ❌ |
| 工具归属到 agent | ✅ `StageContext.agent_id` | ❌（工具事件无归属） |
| usage / 预算归集 | ✅ `_aggregate_crewai_usage` | ❌ **完全不上报 usage** |
| `graph_state` 落库 | ✅ `_persist_graph` | ❌ |
| 计划审批门 | ✅ `_await_plan_confirmation` | ❌ |

`AGENT_WORKFLOW_ENGINE` 默认 `""`（`core/config.py:207`），`.env` 未设——引擎在生产零执行。`orchestrator.py:27-33` 明确记录该路由为 "INTENTIONALLY DEFERRED"。

### 1.2 三个已实证的缺陷

引擎路径虽然未启用，但它是一条**会静默出错**的路径：

1. **计费漏账**。`_run_engine_path` 的 `ev_done` 不带 `usage`（`orchestrator.py:526`），而 `usage` 是 credits 结算的依据。一旦启用引擎且未修，每个 deep_research 轮次都会产生未计费的 token 消耗。
2. **面板"并行中"永远不显示**。`ev_run_status(current_agent_ids=[])` 恒为空列表（`orchestrator.py:406,517`），前端 `activeAgentIds` 由该字段驱动。
3. **链路刷新即丢**。引擎路径从不写 `AgentRun.graph_state`，客户端重连或刷新后右侧面板无内容。

### 1.3 根因

环境搭建**散落在 `CrewAIRuntime.stream_turn` 内部**（`crewai_runtime.py:527-615`：`make_stage_context` → provider/`assistant_msg`/续写检查点装配 → `ApprovalBridge` → `AgentLifecycleEmitter`），没有可复用入口。引擎路径只能拿到一个裸的 `make_stage_context(...)`（`orchestrator.py:543`），于是上述一切能力都缺失。

**所以「接通引擎」的实质不是翻 flag，而是把那套执行环境抽成共享件。**

### 1.4 用户可见的交互缺陷

- 答案前长时间零输出：`StreamingWriterExecutor` 只处理 writer（`streaming_writer.py:107` 的 `_WRITER_AGENT_IDS = {"writer"}`），检索 / 分析 / 辩论全程气泡内只有「思考中…」。
- 中间 Agent 产出不可见：`CrewAIStageExecutor(summarize_chars=160)`（`stage_executor.py:102`）把产出压到 160 字，且该摘要只在面板折叠态出现。
- 节点无耗时 / 成本归属。

## 2. 目标与非目标

### 目标

1. 抽出 **`RunEnvironment`**，让两套 walker 共用同一套执行环境与事件词汇。
2. 引擎路径补齐至与 walker **同级**：审批桥、预算、工具归属、usage 归集、`graph_state` 落库、拓扑来自 plan。
3. 落地 C 类可见性：中间产出全文可见、运行中心跳、节点耗时 / tokens / 成本。
4. 全程可回滚，不改变现有已启用行为。

### 非目标（明确划出）

- **不迁移任何 profile 到引擎**。引擎路由仍由 `AGENT_WORKFLOW_ENGINE` 控制且默认关。迁移是子项目 2。
- **不做非 writer stage 的逐 token 流式**。见 §7.1 的取舍论证。
- **不打开计划审批门**，不给辩论加 UI 入口（子项目 2）。
- **不引入 LLM 规划器 / LLM verifier / 新拓扑**（子项目 3）。
- 不新增数据库表、不新增迁移。

## 3. 决策汇总

| 维度 | 决策 |
|---|---|
| 架构方向 | 方案 A · 引擎为骨：抽共享 `RunEnvironment`，逐 profile 迁移（本子项目只做抽取与对齐） |
| 中间产出粒度 | **全文可见**，单条上限 20 000 字符，超出截断并标记 `truncated` |
| C 类新事件默认值 | **默认开**（只增不改、老客户端忽略、回滚成本为零） |
| 非 writer 流式 | 不做；以「完成即全文 + 运行中心跳」替代 |
| 迁移 | 零迁移 |
| 后端门禁 | `ruff check` 全绿；新增测试全绿 |

## 4. 架构：`RunEnvironment`

新建 `app/agents/run_environment.py`。

> 命名说明：`app/agents/environments.py` 是 Codex 风格的 workspace 环境（cwd / shell / ready 状态），与本类无关。本类是**一次 run 的共享执行环境**。

```python
class RunEnvironment:
    """一次 run 的共享执行环境：stage_ctx + emitter + 预算 + 审批 + 产出归集。

    这是两个 walker（CrewAIRuntime._walk_stages 与 WorkflowEngine）唯一的
    共同入口。它不重新实现 StageContext / AgentLifecycleEmitter —— 那两个类
    保持原样，本类只收归「谁在什么时候调它们」。
    """

    @classmethod
    def for_turn(cls, ctx: AgentTurnContext, run: AgentRun) -> "RunEnvironment":
        """唯一构造器。从 stream_turn 抽出，两个 runtime 都调它。"""

    # ---- 两个 walker 共用的生命周期接口 ----
    def begin(self, graph: AgentGraph) -> None
    def step_started(self, step_id: str, *, title: str | None = None) -> bool
    def step_output(self, step_id: str, text: str) -> None
    def step_completed(self, step_id: str, *, output: str, usage: dict | None) -> None
    def step_failed(self, step_id: str, *, error: str) -> None
    def finish(self, status: str) -> None
    def aggregate_usage(self, results: Mapping[str, StageResult]) -> dict | None
```

### 4.1 职责边界

`RunEnvironment` **持有**而非继承：
- `stage_ctx: StageContext` —— 已填好 `budget_guard` / `provider` / `model_config` / `assistant_msg` / `approval_bridge` / `persistence_*`
- `emitter: AgentLifecycleEmitter` —— 图状态与 `agent_status`/`agent_edge`/`run_status`
- `approval_bridge: ApprovalBridge`
- `guard: BudgetGuard`

### 4.2 行为等价性

`CrewAIRuntime.stream_turn` 改为委托 `RunEnvironment.for_turn`，`_walk_stages` 内的 `emitter.*` 调用改为 `env.*`。这是一次**纯重构**，不改变事件种类、顺序或载荷。等价的判据是**现有测试全绿**，特别是：

- `test_agent_phase0..5.py`
- `test_debate.py`、`test_agent_graph_lifecycle.py`
- `test_approval_bus.py`、`test_durable_controls.py`
- `test_streaming_writer.py`、`test_stage_executor_admission.py`

重构**不带 flag**：等价重构靠测试而非灰度保证，加 flag 只会制造两倍路径。

## 5. `graph_from_plan`

`app/agents/graph.py` 新增：

```python
def graph_from_plan(plan: Plan) -> AgentGraph:
    """从 plan 构建拓扑。Step.dependencies → edges；Step.name/role/
    task_description → 节点字段；stage 由拓扑深度推得（保持面板分层）。"""
```

映射规则：

| `AgentGraphNode` 字段 | 来源 |
|---|---|
| `id` | `Step.id` |
| `name` | `Step.name or Step.id` |
| `role` | `Step.role` |
| `task_title` | `Step.name or Step.id` |
| `task_summary` | `Step.task_description`（截断至 200 字符） |
| `stage` | 拓扑深度（最长依赖链长度），供面板分层与并行分组 |
| `lane` | 同 stage 内的声明顺序 |

边的方向为 `dependency → step`，`type = handoff`，初始 `pending`——这使 `AgentLifecycleEmitter.emit_agent_started` 的 **join 守卫**（`lifecycle.py:117-126`）自然生效，无需为引擎路径另写一套就绪判断。

**等价性约束**：对 `deep_research` 与 `parallel_research` 两个 profile，`graph_from_plan(build_*_plan(q))` 产出的节点 id 集合、依赖关系、stage 分组必须与 `build_*_graph(q)` 完全一致。这条写成参数化测试，是本节的核心验收标准。

## 6. 引擎路径的三处修复

`orchestrator._run_engine_path` 改用 `RunEnvironment`，从而：

1. **usage 归集**。`_run_engine_path` 在引擎返回后调用 `env.aggregate_usage(...)`，并把结果放进 `ev_done(usage=...)`。修掉 §1.2 缺陷 1。
2. **`current_agent_ids` 真实化**。`ev_run_status` 交由 `AgentLifecycleEmitter.emit_run_status` 发出（它调 `graph.recompute_active()`）。修掉缺陷 2。
3. **`graph_state` 落库**。复用 `_persist_graph`，在结构事件（`agent_graph`/`agent_status`/`agent_edge`/`run_status`）后写快照。修掉缺陷 3。

另外，现 `_EmittingExecutor`（`orchestrator.py:424-465`）里手搓的 `ev_step_started`/`ev_agent_status` 事件块**删除**，改由 `env.step_started` / `env.step_completed` / `env.step_failed` 统一发出。这消除了「引擎路径自造一套事件」这个第二真相源。

### 6.1 引擎的 hook 注入

`WorkflowEngine` 接受一组**只读回调**（不改变引擎调度语义）：

```python
WorkflowEngine(
    executor=..., verifier=...,
    on_step_start=env.step_started,       # (step_id, title) -> bool
    on_step_end=env.step_completed,       # (step_id, output, usage) -> None
    on_step_error=env.step_failed,
)
```

回调在 `_run_with_retries` 的成功 / 失败路径上调用（`engine.py:279-312`）。回调自身抛出的异常被吞掉并记日志——**观测绝不能影响执行**，与引擎既有的 best-effort 持久化策略一致。

> 本子项目**不**把暂停 / 恢复 / 追加指令下沉到引擎。控制消费与 profile 迁移一起做（子项目 2），因为在此之前引擎路径对用户不可达。

## 7. C 类：过程可见性

### 7.1 取舍：为什么不做非 writer 的逐 token 流式

`CrewAIStageExecutor.execute` 调 `agent.aexecute_task`（`stage_executor.py:134`），CrewAI 内部消化整个生成过程，不暴露增量。唯一能流式的 writer 之所以可行，是因为 `StreamingWriterExecutor` **绕开 CrewAI 直接调 provider**（`streaming_writer.py:107`）。让检索 / 分析 / 辩论 stage 也绕开 CrewAI，意味着丢掉 CrewAI 的 task 框架与工具循环——改变 stage 语义，风险不成比例。

替代手段在信息量上更强：用户拿到的是**检索到的完整证据**，而不是正在生成的半句话。

### 7.2 新增事件

| 事件 | 时机 | 载荷 |
|---|---|---|
| `step_output` | 任一 stage 完成时 | `run_id`、`agent_id`、`text`（≤20 000 字符）、`truncated: bool`、`chars: int` |
| `step_progress` | 运行中心跳，默认 5s | `run_id`、`agent_id`、`elapsed_s`、`note`（如「最近工具：web_search」） |

`ev_agent_status` **扩展**（纯新增可选字段，`schemas.py:342-368`）：
- `usage: dict | None` —— 该 stage 的 tokens
- `cost_usd: float | None` —— 该 stage 的成本

`output_summary`（160 字）保留为**折叠态摘要**，`step_output` 的全文是**展开态**——分层，不是替换。

### 7.3 心跳实现

心跳只在 stage 运行期间存在：`step_started` 时起一个 `asyncio.Task`，每 `AGENT_STEP_PROGRESS_INTERVAL_S`（默认 5.0）发一次 `step_progress`，`step_completed`/`step_failed` 时取消。

`note` 的来源是**已有状态而非新增簿记**：`AgentLifecycleEmitter.set_current_tool`（`lifecycle.py:244`）已经把当前工具写到图节点上，心跳读 `graph.node(agent_id).current_tool["name"]` 即可；无工具时为「已运行 Ns」。

心跳任务登记在 `RunEnvironment` 上，`finish()` 与 run 取消时统一取消——**不允许泄漏后台任务**（`finally` 保证）。

### 7.4 flag

`AGENT_RICH_STEP_EVENTS: bool = True`（`core/config.py`）作为**总开关**，同时控制 `step_output`、`step_progress`、节点 `usage`/`cost_usd`。默认开；置假即回到当前行为。因为全部是**增量事件与增量字段**，老客户端忽略它们，回滚成本为零。

`AGENT_STEP_PROGRESS_INTERVAL_S: float = 5.0` 单独可调。

## 8. 前端

### 8.1 类型与 reducer

`lib/agent-graph-types.ts` 的 `AgentGraphNode` 增加可选字段（`durationMs` / `currentTool` 已存在，无需新增）：
- `outputFull?: string`
- `outputTruncated?: boolean`
- `usage?: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number }`
- `costUsd?: number`
- `progressNote?: string`

`lib/agent-graph-reducer.ts` 新增两个 action：
- `STEP_OUTPUT` → 写 `outputFull` / `outputTruncated`
- `STEP_PROGRESS` → 写 `progressNote`

`usage` / `costUsd` **不新增 action**：它们随扩展后的 `ev_agent_status` 到达，由既有的 `AGENT_STATUS` patch 通道写入（`agent-graph-reducer.ts:79-90`）。

两个新 action 都走既有的「不回归」守卫：terminal 节点仍接受非状态字段合并（`mergeNonStatus`，`agent-graph-reducer.ts:158`），所以 stage 完成后到达的产出不会丢。

`hooks/useChatStream.ts` 的 SSE 桥（现约 `:376-400`）增加 `step_output` / `step_progress` 两个 handler。

### 8.2 组件

- **`agent-node-card.tsx`**：展开区渲染 `outputFull`（Markdown，滚动容器，截断时显示「已截断」提示）+ `durationMs` + `usage` + `costUsd`。
- **`agent-inline-status.tsx`**：从「X、Y 并行中」升级为带进度的实时行——运行中「检索中 23s · 最近：web_search」（数据取 `durationMs` 与 `currentTool.name`）；完成后「✓ 检索完成 · 12.3s」。数据全部来自 store，**不编造前端模拟的计数**。
- **`agent-activity-feed.tsx`**：消费 `step_output`，把阶段产出作为活动流条目。

沿用既有规范：动画遵守 `prefers-reduced-motion`；不做横向链路 / DAG 缩放。

## 9. 关键文件清单

**后端 · 新增**
- `app/agents/run_environment.py` —— `RunEnvironment`
- `tests/test_run_environment.py`
- `tests/test_graph_from_plan.py`

**后端 · 修改**
- `app/agents/runtime/crewai_runtime.py` —— `stream_turn` 改为委托 `RunEnvironment.for_turn`；`_walk_stages` 改调 `env.*`
- `app/agents/graph.py` —— 新增 `graph_from_plan`；`AgentGraphNode` 增加 `usage` / `cost_usd` 可选字段
- `app/agents/orchestrator.py` —— `_run_engine_path` 使用 `RunEnvironment`；删除 `_EmittingExecutor` 的手搓事件；修 usage / `current_agent_ids` / `graph_state`
- `app/agents/workflow/engine.py` —— 接受 `on_step_*` 只读回调
- `app/agents/schemas.py` —— 新增 `ev_step_output` / `ev_step_progress`；扩展 `ev_agent_status`
- `app/core/config.py` —— `AGENT_RICH_STEP_EVENTS`、`AGENT_STEP_PROGRESS_INTERVAL_S`
- `tests/test_engine_routing.py` —— 扩展

**前端 · 修改**
- `src/lib/agent-graph-types.ts`、`src/lib/agent-graph-reducer.ts`
- `src/hooks/useChatStream.ts`
- `src/components/agents/agent-node-card.tsx`、`agent-inline-status.tsx`、`agent-activity-feed.tsx`

## 10. 测试

**后端**
- `RunEnvironment.for_turn` 装配完整（provider / assistant_msg / approval_bridge / budget_guard 均非空）。
- **行为等价**：改造后既有 `test_agent_phase0..5.py` / `test_debate.py` / `test_agent_graph_lifecycle.py` / `test_approval_bus.py` / `test_streaming_writer.py` 全绿。
- **拓扑等价**：`graph_from_plan(build_deep_research_plan(q))` 与 `build_deep_research_graph(q)` 的节点集 / 边集 / stage 分组一致；`parallel_research` 同。
- **引擎路径事件一致**：同一 profile 下，引擎路径与 walker 发出的事件 **kind 集合**相同。
- **usage 非空**：引擎路径的 `ev_done.usage` 非空且含 tokens。
- **`current_agent_ids` 非空**：并行阶段（`advocate-a` ‖ `advocate-b`）的 `run_status` 同时列出两个 id。
- **心跳**：运行期间产生 ≥1 个 `step_progress`；stage 结束后心跳任务被取消（无遗留 task）。
- **产出透出**：`step_output.text` 等于 stage 真实产出；超 20 000 字符时 `truncated=True` 且 `len(text) == 20000`。
- `AGENT_RICH_STEP_EVENTS=False` 时不发新事件。

**前端**
- reducer：`STEP_OUTPUT` / `STEP_PROGRESS` 更新节点；terminal 节点仍接受产出合并。
- 组件快照：`agent-node-card` 展开态（含 usage / 成本 / 截断提示）；`agent-inline-status` 运行中与完成两态文案。

## 11. 验收标准

1. 抽 `RunEnvironment` 后，既有后端测试全绿，无事件载荷变化。
2. 引擎路径（`AGENT_WORKFLOW_ENGINE=1`）跑一次 deep_research：`ev_done.usage` 非空、`run_status.current_agent_ids` 在并行段非空、`AgentRun.graph_state` 已落库。
3. 一次 `expert` 模式 deep_research 运行中，右侧面板可**展开任一已完成 stage 读到完整产出**，并显示其耗时与 tokens。
4. 运行中气泡内显示带进度的心跳行，而不是静态「思考中…」。
5. 全程不改变任何已启用路径的事件种类与顺序（由 1 保证）。
6. `ruff check app tests` 全绿。

## 12. 后续子项目（本设计的边界外）

- **子项目 2**：逐个 profile 迁移到引擎（带 flag 灰度）；暂停 / 恢复 / 追加指令下沉到引擎；打开计划审批门；辩论的 UI 入口。
- **子项目 3**：LLM 规划器替换模板 planner；LLM verifier 替换只查字数的 `RuleBasedVerifier`；新拓扑（任务分解 / 写-审-改 / 代码协作）。
