# 多 Agent 能力强化 · 子项目 2+3 设计（引擎接管 + 规划/验收智能化）

- 日期：2026-09-18
- 状态：已批准（Approved）
- 范围：backend（主）+ frontend
- 上游：子项目 1（`2026-09-18-agent-run-environment-design.md`，已实施并已上生产）。本设计承接其「不迁移 profile、不开审批门、不加辩论入口、不引入 LLM 规划器」四项目标外事项。

## 1. 背景与现状

子项目 1 已完成：`RunEnvironment` 抽出并双 walker 共用；引擎路径补齐审批桥/预算/工具归属/usage 归集/`graph_state` 落库/拓扑来自 plan；中间产出全文可见 + 运行中心跳 + 节点耗时/tokens/成本。引擎路径从「不可上线」变为「可上线但默认关」（`AGENT_WORKFLOW_ENGINE` 默认 `""`）。

本设计解决剩下的三块：**接管**（profile 真正跑在引擎上）、**可控**（暂停/恢复/指令下沉 + 计划审批门 + 辩论入口）、**更强**（LLM 规划器、LLM verifier、新协作拓扑）。

### 1.1 现状盘点

| 能力 | 现状 |
|---|---|
| 引擎调度（DAG / 有界并发 / 重试分级 / verify→replan / attempt 落库） | 已建成，**仅 `deep_research` 可路由，且 flag 默认关** |
| `_TRANSIENT` 重试策略 | 已启用（`max_retries=1`，`planner.py:39`） |
| 运行控制（暂停/恢复/取消/追加指令） | walker 有；**引擎路径零消费**（`run_control` 已在 `ctx.extra`，`orchestrator.py:128`） |
| 计划审批门 | 后端就绪（`_await_plan_confirmation` + `/plan/confirm`），`PLAN_REQUIRE_CONFIRMATION` 默认 `False` |
| 辩论 profile | 后端完整（`crews/debate.py`：双 advocate 并行 → judge join）；**前端无入口** |
| 规划器 | 纯模板（`planner.py`，无 LLM 入口） |
| verifier | `RuleBasedVerifier` 只校验 `min_chars`（`verifier.py:71`）—— 系统最弱一环 |
| 协作拓扑 | 3 个（deep_research / parallel_research / debate），全是「检索→写作」或「辩论」 |

## 2. 目标与非目标

### 目标

1. **堵住已实证的重试 × 终态守卫缺陷**（见 §3），它是现网即触发的问题，且是 profile 迁移的前提。
2. 运行控制下沉到引擎：暂停 / 恢复 / 取消 / 追加指令。
3. 三个 profile 迁移到引擎（按名单灰度，可独立回滚）。
4. 辩论获得真实 UI 入口；计划审批门默认开启。
5. LLM 规划器与 LLM verifier（均带确定性回退）。
6. 新增两个协作拓扑（任务分解 / 写-审-改），并纳入自动路由。

### 非目标

- 不做非 writer stage 的逐 token 流式（子项目 1 §7.1 的论证不变）。
- 不改 native 单 Agent 路径。
- 不引入新数据库表、不新增迁移。
- 不做 verifier 的「自动修正」——`revise` 只触发重跑与整合，不改写他人产出。

## 3. 先决修复：重试 × 终态守卫（已实证）

### 3.1 缺陷

`AgentLifecycleEmitter.emit_agent_completed`（`lifecycle.py:153-184`）的幂等检查**只挡 `completed`**（`lifecycle.py:164`），不挡 `failed`。而 `_TRANSIENT.max_retries=1`（`planner.py:39`）意味着引擎的 transient 失败会重试。

实测序列（已用探针复现）：

```
attempt1 失败 → emit_agent_failed → node.status = failed（终态）
attempt2 成功 → emit_agent_completed → node.status = completed   ← 翻回
```

实测结果，节点同时呈现三个矛盾状态：

| 字段 | 值 | 问题 |
|---|---|---|
| `status` | `completed` | — |
| `error` | `"Connection error."` | **失败信息未清除** |
| `duration_ms` | `0` | **起始时间戳在失败时被 pop，未恢复** |

用户看到的是「已完成、但带着错误、耗时 0ms」的节点。**这是现网即触发的**：引擎路径一启用就会遇到，与是否迁移 profile 无关（`AGENT_WORKFLOW_ENGINE=1` 的 deep_research 已经会触发）。

### 3.2 修复（`lifecycle.py`）

三处改动：

1. **终态守卫**：`emit_agent_completed` 只在 `node.status == cancelled` 时拒绝（用户主动取消是真正的终态）；`failed` **允许**被覆盖——重试成功本就该覆盖失败。
2. **清残留**：成功时置 `node.error = None`。
3. **新增 `emit_agent_retrying(agent_id, *, attempt, error)`**：把节点从 `failed` 翻回 `running`，并写 `node.retrying = {"attempt": N, "error": "..."}`。发 `agent_status`（status=`running`，带 `retrying` 载荷），让面板显示「第 2 次尝试」而不是节点诈尸。

配套：`emit_agent_failed` **不再 pop** `_node_starts`（保留起始时间戳，使重试后的 `duration_ms` 是**从首次开始**的累计耗时，语义上更接近用户感知）；真正的终态结算由 `emit_agent_completed`/`emit_agent_cancelled` 负责。

### 3.3 引擎侧

`WorkflowEngine` 的 transient 分支（`engine.py:314-317`）在 `continue` 之前调用新增的第 4 个只读回调：

```python
on_step_retry(step_id: str, attempt: int, error: str) -> None
```

由引擎路径接到 `env.step_retrying`。

## 4. 运行控制下沉到引擎

### 4.1 为什么需要新钩子

现有 `on_step_start` 是**只读观测**（异常被吞）。而暂停/取消必须**能中断**执行。所以新增一个语义不同的钩子：

```python
before_step(step_id: str) -> None   # async；抛异常即中断本 step
```

`WorkflowEngine` 在 `run_step` 内、等待依赖之后、真正执行之前 await 它。抛出的 `CancelledError` 让引擎终止（与用户取消一致）。

### 4.2 `RunEnvironment.respect_controls()`

新增 async 方法，语义与 walker 的 `_respect_controls`（`crewai_runtime.py`）对齐：

- `ctl.cancel.is_set()` → 抛 `CancelledError`
- `ctl.is_paused()` → 发 `run_paused` 事件，循环等待直到恢复或取消（带预算超时保护）
- 排空追加指令（`drain_instructions()`）→ 发 `run_instruction_received`，写入 `stage_ctx.pending_instructions`

控制来源仍是 `ctx.extra["run_control"]`（orchestrator 已注册）。**持久命令 drain**（`_drain_durable_commands`）也一并下沉，使 `/pause` `/resume` API 在引擎路径真实生效。

### 4.3 注入方式

引擎路径把 `before_step=env.respect_controls` 传给 `WorkflowEngine`。`RunEnvironment` 已是 env 的一部分，无需新依赖。

## 5. profile 迁移（名单式灰度）

### 5.1 配置

`AGENT_WORKFLOW_ENGINE`（总开关，默认 `""` = 关）保持不变；新增：

```python
# 逗号分隔的 profile 名单。总开关为真时，只有名单内的 profile 走引擎。
# 空字符串 = 名单为空 → 没有任何 profile 走引擎（安全默认）。
AGENT_WORKFLOW_ENGINE_PROFILES: str = ""
```

路由判据从 `_should_route_to_engine` 的「flag 且 profile == deep_research」改为「flag 且 profile ∈ 名单」。

### 5.2 迁移顺序与依据

| profile | 拓扑等价性依据 | 备注 |
|---|---|---|
| `deep_research` | 子项目 1 的 `test_graph_from_plan.py` 已断言节点集/边集/stage/lane/中文文案全等 | 首个迁移 |
| `parallel_research` | 同上（`test_parallel_research_topology_and_presentation_match`） | 第二个 |
| `debate` | `graph_from_plan` 对 debate 有 stage/lane 断言；judge 是 join，与 emitter 的 join 守卫同构 | 需 §6.1 的 UI 入口同步落地 |

每个 profile 可单独从名单移除 = 独立回滚。**上线默认值保持 `""`**，即行为不变；开启是显式动作。

### 5.3 迁移必须同时满足的条件

1. §3 的重试缺陷已修（否则重试即产生矛盾状态）。
2. §4 的控制下沉已就绪（否则用户按暂停无反应——这比没有按钮更糟）。
3. 引擎路径与 walker 的事件词汇一致（子项目 1 已有 `test_engine_and_walker_emit_the_same_lifecycle_event_kinds` 把守）。

## 6. 交互入口

### 6.1 辩论入口

`user-modes.ts` 新增第 4 个模式：

```
debate 辩论 —— 双 Agent 并行论证 + 中立裁判
```

后端 `VALID_MODES` 与 `decide_route` 的 `debate` 分支**已存在且完整**，本项是纯前端补入口。`SPECIAL_MODES` 加入 `debate`（composer 显示模式徽章）。`chat-ui-store.ts` 的 `mapLegacyMode` 已把 v3 的 `debate` 映射为 `expert`——改为映射回 `debate`。

### 6.2 计划审批门：计划先行，默认不阻塞

`PLAN_REQUIRE_CONFIRMATION` 默认由 `False` 改为 **`True`**，但**语义升级**为「计划先行」：

- 计划**总是**发布（`research_plan` 事件 + 落库），UI 立即可见、可修改、可阻止。
- **默认不阻塞**：计划发布后运行立即继续。生产系统的默认行为不能让用户干等——多数用户要的是答案，不是审批流程。
- **用户可主动上闸**：PlanReview 提供「暂停执行」；点击后在下一次 step 边界进入门禁（复用 §4 的 `before_step` 控制点），等用户确认/修改后再继续。
- **`PLAN_CONFIRM_TIMEOUT_S`** 默认 **`90`**（由 300 降）。仅在用户**已经**上闸时才可能等待；超时后按计划继续，并发出说明事件（面板可见），不让门禁被无声忽略。

这样「门」是真实存在的（可阻断、可修改、有超时兜底、有说明），但默认路径零等待。相比「默认阻塞 90 秒」，这是生产环境下唯一可接受的默认值。

引擎路径需接入同一门（子项目 1 未做，其 spec §6.1 已声明延后到此）。`RunEnvironment.respect_controls()`（§4.2）在消费控制时一并检查「用户是否上闸」，因此两个 walker 共享同一套门禁逻辑。

## 7. 子项目 3：规划与验收智能化

### 7.1 LLM 规划器

新建 `workflow/llm_planner.py`：

```python
async def build_plan_with_llm(
    *, ctx: AgentTurnContext, profile: str, question: str,
) -> Plan
```

- 用 `ctx.model_config`（用户当前模型）发**一次**结构化请求，要求返回 JSON 形态的步骤列表。
- **硬约束**：产物必须通过 `validate_plan()`（无环、依赖存在、id 唯一）。任一校验失败 → 回退到模板 planner（`build_plan_for_profile`）。
- 模型调用失败/超时 → 同样回退。**LLM 永远不能让引擎挂掉**。
- 步数上限（默认 8）与单步描述长度上限，防止模型产出超大 plan。

flag：`AGENT_LLM_PLANNER`（默认 `False`，先关后开；见 §9）。

### 7.2 LLM verifier

新建 `workflow/llm_verifier.py`：

```python
class LLMVerifier:
    async def verify(self, plan: Plan, observations: dict[str, StepObservation]) -> VerifierResult
```

- 输入：各步的**产出**（`output`）与其 `acceptance_criteria`——不给隐藏推理。
- 输出：`pass` / `revise`（带 `revise_step_ids`）/ `fail` + `findings`。
- **硬约束**：模型返回的 verdict 不在枚举内、或 `revise_step_ids` 含未知 step → 回退 `RuleBasedVerifier`。
- 保留 `RuleBasedVerifier` 作为可配置回退与测试替身。

flag：`AGENT_LLM_VERIFIER`（默认 `False`）。

### 7.3 新协作拓扑

两个新 profile，均按现有 `build_*_stages` 的形状实现（CrewAI `Agent`/`Task` + `StageSpec` + `build_*_graph` + plan 模板）：

**`task_decomposition`** —— 协调者 → worker×N 并行 → 整合者

```
decomposer (stage 0)
  → worker-1..N (stage 1, 并行, 依赖 decomposer)
  → integrator (stage 2, join 全部 worker)
```

worker 数量由 LLM 规划器决定（回退模板时默认 3）。

**`write_review`** —— 初稿 → 审阅 → 定稿

```
drafter (stage 0) → reviewer (stage 1) → finalizer (stage 2)
```

审阅者输出结构化问题清单，定稿者据其修改。

两者都需：`graph.py` 的静态 builder（提供中文展示文案，供 `graph_from_plan` 复用）、`planner.py` 的模板、`crews/` 的 stage builder、前端 `agent-run-header.tsx` 的 profile 标签。

### 7.4 自动路由（含风险控制）

新 profile 纳入 `auto` 模式的意图升级，但有三道闸：

1. **显式模式永不升级**：`speed` 与显式指定的模式不走自动升级（沿用 `decide_route_with_intent` 的 `speed` 短路）。
2. **置信度门槛**：沿用 `_INTENT_MIN_CONFIDENCE = 0.5`；低于此不升级。
3. **长度门槛**：沿用 `_AUTO_MULTI_MIN_LEN`，避免「总结下」这类短提问被拉进多 Agent。

意图分类的扩展：`IntentDecision.deliverable_kind` 已有 `code`/`document`/`factual`；新增路由提示的判定放在 `decide_route_with_intent`，由模型给出的 `route` 值决定（`task_decomposition` / `write_review`）。

**误判的代价**：一次误升级 = 用户多等几十秒。所以新 profile 的自动升级**只认模型给出的显式 route**，不做关键词猜测——`decide_route`（纯关键词路径）**不**新增 `task_decomposition` / `write_review` 的匹配规则；只有 `decide_route_with_intent`（模型判断路径）在模型明确点名时才升级。这样「纳入自动路由」的能力来自模型的意图识别，而不会因为用户随手写了「任务」「审阅」这类词就误触发。

## 8. 预算、计费与可观测性

### 8.1 预算与计费

- LLM 规划器与 verifier 的 token 走 **`RunEnvironment.guard`**（`ctx.extra["budget_guard"]`），与 stage 同源，usage_id 分别为 `crewai:planner:{run_id}` 与 `crewai:verifier:{run_id}`。
- 二者计入 `AGENT_MAX_TOTAL_TOKENS` / `AGENT_MAX_COST_USD`，且 `guard.check()` 在调用前执行——预算耗尽时规划器直接回退模板，不阻断运行。
- `aggregate_usage` 必须把它们并入，**避免计费漏账**（子项目 1 修过同类缺陷，不能再犯）。
- **并发安全约束（实施时不可违反）**：`BudgetGuard` 内部用 `threading.RLock`（`budget_policy.py:124`）保护临界区，因为工具在 worker 线程里调用它。规划器/verifier 在主事件循环上调用同一 guard——这在 RLock 下是正确的，**前提是临界区内不得出现 `await`**。实施时任何新增的「先 check 再 add_usage」都必须保持同步，中间不能 yield。

### 8.2 可观测性（生产必备）

LLM 规划器与 verifier 是**额外的模型调用**，它们失败时必须能从指标看出来——只写 `logger.warning` 在生产上等于不可观测。复用仓库既有的 `observe_counter` / `observe_span`（引擎已在用，`engine.py`）：

| 指标 | 类型 | 标签 | 用途 |
|---|---|---|---|
| `agent.llm_planner` | counter | `outcome` ∈ `ok` / `invalid_plan` / `error` / `timeout` / `budget_exhausted` | 规划器成功率与失败归因 |
| `agent.llm_verifier` | counter | `outcome` ∈ `ok` / `invalid_verdict` / `error` / `timeout` | verifier 成功率与失败归因 |
| `workflow.step.retry` | counter | `step_id` | 重试频次（配合 §3 的修复观测重试是否健康） |
| `agent.engine.profile` | counter | `profile` | 哪些 profile 真的跑在引擎上（灰度可见性） |

span：`agent.llm_plan` 与 `agent.llm_verify`，属性只带 `profile` / `step_id` / `duration_ms`——**绝不带 prompt 或产出的正文**（沿用仓库的脱敏约定，见 `observe_span` 的 redacted attributes 说明与 `test_observability_redaction.py`）。

### 8.3 延迟预算

规划器调用发生在**用户等待期**（首 token 之前），所以它直接增加首 token 延迟。生产约束：

- `AGENT_LLM_PLANNER_TIMEOUT_S` 默认 **`8`**（含重试的总预算，不是单次）。超时即回退模板 plan，运行继续。
- verifier 调用发生在步骤之间，同样受限：`AGENT_LLM_VERIFIER_TIMEOUT_S` 默认 **`10`**。
- 两者都**不重试模型调用**（重试会让延迟翻倍，而回退路径本身就是可用的）。失败即回退。
- 规划器的 `max_tokens` 限制（默认 1024）：plan 是结构化短输出，不需要长生成。

## 9. flag 与默认值汇总

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `AGENT_WORKFLOW_ENGINE` | `""`（关） | 总开关，保持关 |
| `AGENT_WORKFLOW_ENGINE_PROFILES` | `""`（空名单） | 显式列出要迁移的 profile |
| `PLAN_REQUIRE_CONFIRMATION` | **`True`**（由 False 改） | 门开着，但默认不阻塞（见 §6.2） |
| `PLAN_CONFIRM_TIMEOUT_S` | **`90`**（由 300 改） | 仅用户已上闸时可能等待 |
| `AGENT_LLM_PLANNER` | `False` | LLM 规划器，先关后开 |
| `AGENT_LLM_VERIFIER` | `False` | LLM verifier，先关后开 |
| `AGENT_LLM_PLANNER_MAX_STEPS` | `8` | 防超大 plan |
| `AGENT_LLM_PLANNER_TIMEOUT_S` | `8` | 首 token 延迟预算 |
| `AGENT_LLM_VERIFIER_TIMEOUT_S` | `10` | 步骤间延迟预算 |
| `AGENT_RICH_STEP_EVENTS` | `True` | 子项目 1 已上，不变 |

## 10. 测试策略

- **重试缺陷**：断言 `failed → retrying → completed` 的完整序列；断言成功时 `error is None`；断言 `duration_ms` 累计而非归零；断言 `cancelled` 不被 `completed` 覆盖。
- **控制下沉**：引擎路径下发 pause 真的停住、resume 真的继续、cancel 真的终止、追加指令真的进入下一步的 `upstream`。
- **profile 迁移**：每个 profile 在名单内时走引擎、名单外时走 walker；两条路径事件种类集合相同。
- **计划门**：门开时暂停等确认、确认后继续、超时后继续且发说明事件。
- **LLM 规划器**：非法 plan（有环 / 依赖缺失 / 重复 id）回退模板；模型异常回退模板；合法 plan 被采用。
- **LLM verifier**：非法 verdict 回退规则版；`revise` 的 `revise_step_ids` 生效。
- **新拓扑**：`graph_from_plan` 与静态 builder 等价；并行 worker 的 join 正确；整合者拿到全部 worker 产出。
- **是否触发新拓扑**：短提问/低置信度不升级；显式 speed 永不升级。

## 11. 验收标准

1. 重试成功后节点无残留 `error`、`duration_ms` 非零、状态为 `completed`；`cancelled` 不被 `completed` 覆盖。
2. 名单内的 profile 在引擎上跑通，且用户可暂停/恢复/取消/追加指令。
3. 计划总是发布且可修改；默认路径**不阻塞**；用户上闸后进入门禁，确认或超时（90s）后继续且面板有说明。
4. 辩论模式出现在模式选择器并真的跑出双 advocate + judge。
5. 开启 LLM 规划器后，非法/失败的模型输出一律回退模板，运行不中断；**首 token 延迟增量 ≤ `AGENT_LLM_PLANNER_TIMEOUT_S`**。
6. 开启 LLM verifier 后，`revise` 触发定向重跑而非全量。
7. 新拓扑在模型明确点名时正确执行；自动路由不升级短提问与低置信度请求，显式 speed 永不升级。
8. **回滚可验证**：把某 profile 从 `AGENT_WORKFLOW_ENGINE_PROFILES` 移除后，该 profile 确实回到 walker 路径（断言事件来源与执行栈），且行为与迁移前一致。
9. **灰度可见**：`agent.engine.profile` 指标能区分「跑在引擎上」与「跑在 walker 上」的轮次，运维无需读日志即可确认灰度状态。
10. **可观测性**：规划器/verifier 的每类失败（`invalid_plan` / `error` / `timeout` / `budget_exhausted`）都有对应 counter 增量；span 属性不含 prompt 或产出正文（由脱敏测试把守）。
11. **计费不漏账**：规划器与 verifier 的 token 计入 `ev_done.usage`，且计入预算上限。
12. `ruff check app tests` 全绿；后端全量测试（含既有 1226 项）无回归；前端 typecheck/lint/test 全绿。
13. 零迁移。

## 12. 风险

| 风险 | 缓解 |
|---|---|
| 审批门让用户困惑「为什么不动了」 | 默认不阻塞（§6.2）；门只在用户主动上闸后生效；90s 超时兜底 + 明确文案 |
| 新拓扑被误路由，用户多等 | 三道闸（显式模式短路 / 置信度 / 长度）；只认模型显式点名，不加关键词规则 |
| LLM 规划器产出坏 plan | `validate_plan()` 强校验 + 全回退路径 |
| LLM verifier 误判导致无谓重跑 | `revise` 受 `max_replans` 约束；非法输出回退规则版 |
| 迁移后行为回归 | 名单式灰度，每个 profile 可独立摘除；引擎路由始终在总开关之后 |
| LLM 规划器拖慢首 token | 8s 超时预算 + 不重试 + 回退即用（§8.3）；有独立开关可整体关掉 |
| 额外模型调用推高成本 | 走同一 guard，计入预算上限；有独立开关；指标可见调用量与失败率 |
| 一次全开导致大面积行为变化 | 见 §12.1 的分阶段发布 |

### 12.1 分阶段发布（生产约束）

本设计的 flag 默认值全部是**安全**的（引擎关、LLM 规划器/verifier 关、名单空），所以**合入 main 不改变任何生产行为**。行为变化全部来自运维显式改 `.env`。发布分四步，每步可独立观测、独立回滚：

| 阶段 | 动作 | 观测 | 回滚 |
|---|---|---|---|
| 1（合入） | 代码上生产，flag 全默认为安全值 | 无行为变化 | — |
| 2 | `AGENT_WORKFLOW_ENGINE=1` + `AGENT_WORKFLOW_ENGINE_PROFILES=deep_research` | `agent.engine.profile` 出现 deep_research；重试指标正常 | 清空名单 |
| 3 | 名单加入 `parallel_research`、`debate` | 同上，加两个 profile | 从名单移除对应项 |
| 4 | `AGENT_LLM_PLANNER=1` / `AGENT_LLM_VERIFIER=1` | 规划器/verifier 的 outcome counter；首 token 延迟 | 关掉对应 flag |

每步之间至少观察一个真实使用周期。唯一**默认改变行为**的一项是 `PLAN_REQUIRE_CONFIRMATION=True`（§6.2）——但它默认不阻塞，所以用户感知是「多了一个可查看可修改的计划」，而不是「卡住了」。

## 13. 关键文件清单

**后端 · 新增**
- `app/agents/workflow/llm_planner.py`、`app/agents/workflow/llm_verifier.py`
- `app/agents/crews/task_decomposition.py`、`app/agents/crews/write_review.py`
- 对应测试：`test_llm_planner.py`、`test_llm_verifier.py`、`test_task_decomposition.py`、`test_write_review.py`、`test_engine_controls.py`

**后端 · 修改**
- `app/agents/lifecycle.py`（§3 重试修复）
- `app/agents/run_environment.py`（`respect_controls`、`step_retrying`）
- `app/agents/workflow/engine.py`（`before_step`、`on_step_retry`）
- `app/agents/workflow/planner.py`（新模板）
- `app/agents/graph.py`（新静态 builder）
- `app/agents/orchestrator.py`（名单路由、控制注入、计划门）
- `app/agents/intent_router.py`（新 profile 路由）
- `app/core/config.py`（§9 的 flag）

**前端 · 修改**
- `src/lib/user-modes.ts`、`src/stores/chat-ui-store.ts`（辩论入口）
- `src/components/agents/agent-run-header.tsx`（profile 标签）
- `src/components/agents/plan-review.tsx`（门开/超时文案）
