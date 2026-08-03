# 多 Agent 协作感知面板 — 设计文档

- 日期：2026-08-03
- 状态：已批准（Approved）
- 范围：frontend（主）+ backend（native 路径补 graph 事件）

## 1. 背景与问题

现状（探索结论）：

- 多 agent 可视化组件**已全部造好**：`AgentFlowGraph`（链路 DAG）、`AgentNodeCard`、`AgentActivityFeed`、`AgentRunHeader`、`AgentStatusBadge`（`NODE_STATUS_META`），配套 zustand store（`agent-run-store.ts`）、纯 reducer（`agent-graph-reducer.ts`）、类型镜像（`agent-graph-types.ts`）、恢复 hook（`useAgentRunGraph.ts`）、SSE 桥（`useChatStream.ts`）。
- 但专用 `MultiAgentPanel` 与 `AgentPanelTrigger` **从未挂载**（孤儿组件）。用户实际看到的只是 `ContextPanel` 里一个「执行」tab（`ExecutionTab`）。**用户感知不到「有多个 agent 在协作」。**
- 后端 CrewAI 多 agent 路径**已发全套** graph 事件（`agent_graph`/`agent_status`/`agent_edge`/`run_status`）；native 路径（auto/search/create/data_analysis/chat）**不发任何** agent 事件，普通对话的面板为空。

目标：让用户清晰感知多 agent 协作 —— 右侧统一面板 + 纵向链路 + 运行/并行/JOIN 状态 + 4 个感知入口；并让 native 路径也有 agent/stage 可视化。

## 2. 决策汇总（与用户确认）

| 维度 | 决策 |
|---|---|
| 面板架构 | **B** 合并为统一右侧面板，Tab = 执行 / 来源 / 文件 |
| 链路朝向 | **A** 纵向（自上而下，同层并行左右排开） |
| 状态编码 | 复用 `NODE_STATUS_META`（蓝=运行/琥珀=等待/绿=完成/灰=排队·取消/红=失败）+ 并行徽标 + JOIN 标记 |
| 自动弹出 | 多 agent 运行 → 自动切「执行」tab；native 单 agent **不强制弹**（避免每条消息弹） |
| 感知入口 | **1+2+3+4 全要**（顶栏触发器 / 气泡内嵌 / 全局进度角标 / 完成提示） |
| native 范围 | **C** 后端 native 路径也发 graph 事件（含 step 生命周期 + tool 归属） |

## 3. 前端架构（方案 B）

- 合并 `MultiAgentPanel` 与 `ContextPanel` 为**单一右侧面板**，Tab：执行 / 来源 / 文件。
  - 执行 tab = 原 `ExecutionTab`（已复用 `AgentFlowGraph`+`AgentActivityFeed`），升级为本设计第 4 节的链路图。
- `app/page.tsx`：挂统一面板；顶栏接 `AgentPanelTrigger`。
- 沿用现有 docked `<aside w-[400px]>`（xl+）+ 移动端 `Sheet side=right` 双模式。
- 修复响应式 bug：`selectShouldShowPanel` 由 `getState()` 快照改为响应式 `useStore(selector)`。
- store 仍只持一个 active graph（不引入并发多 run）。

## 4.「执行」Tab 链路图（方案 A 纵向）

- **复用**：`AgentFlowGraph`（纵向 stage 分层）、`AgentNodeCard`、`AgentActivityFeed`、`AgentRunHeader`、`AgentStatusBadge`。
- 状态：复用 `NODE_STATUS_META`；运行节点 `agent-node-running` 脉冲；并行同层并排 + 「N 并行」徽标；JOIN 节点琥珀「等待上游」。
- **前端增强**：
  1. 头部显示 `GraphMode` 标签（顺序/并行/混合）——数据已有，UI 未显示。
  2. 同层并行节点加分组框（视觉强调并行组）。
  3. 节点卡可展开查看 `taskSummary` / `outputSummary` / `error`。
- 动画遵守 `prefers-reduced-motion`（现有规范）。

## 5. 四个感知入口

1. **顶栏常驻触发器**：复活 `AgentPanelTrigger`——运行时脉冲药丸「多 Agent · N 运行中」，一键切/重开面板；native 单 agent 运行时显示「助手 · 运行中」。
2. **气泡内嵌实时状态**：助手消息气泡内渲染「X、Y 并行中…」+「展开」按钮，点击切「执行」tab。基于 `activeAgentIds`/`run_status` 派生；native 单 agent 显示当前 step/工具。
3. **全局迷你进度角标**：`AppShell` 侧栏加常驻进度环 + 运行数角标，跨页面可见。
4. **完成轻提示条**：运行结束面板顶部一条「✓ 完成 · N Agent · 耗时」，可一键回看链路。

## 6. 后端改动（范围 C）

修改 `backend/app/agents/runtime/native_runtime.py`（单 agent 路径）：

- 运行开始：发 `agent_graph`（合成**单节点图**：一个「助手」节点，stage 0；可加 `build_single_agent_graph` 辅助）+ `agent_status:running` + `run_status{current_agent_ids:["assistant"]}`。
- `tool_call`/`tool_result`：**补 `agent_id`**（当前 native 不带），使其可挂到节点下。
- 激活**当前为死代码**的 `step_started`/`step_completed`（`schemas.py` 已定义、零调用点）：native 循环逐步发出，让单 agent 也有逐步进度。
- 运行结束：发 `agent_status:completed` + `run_status:completed`。
- 多 agent 路径（CrewAI）**完全不动**。前端 `useChatStream` 已有全部 handler，零前端消费改动。

## 7. 状态 / 空 / 错误 / 演示

- 空态（无运行）：「执行」tab 显示引导插画 + 文案「发起深度研究 / 辩论即可看到多 Agent 协作」。
- 演示态：复用 `selectIsDemo`，链路图顶部加「演示模式，内容非真实生成」徽标。
- 错误：节点红 + `error` 文案；SSE 断线用现有 `GET /api/agent-runs/{id}` 兜底恢复。
- 所有动画遵守 `prefers-reduced-motion`。

## 8. 不做（YAGNI）

- 不做并发多 run 浏览（store 只持一个 active）。
- 不做横向链路、DAG 缩放/平移（2–8 节点用不上）。
- 不做 token 归属到具体节点、每节点 token 进度条（`ev_token` 不带 agent_id，改动大收益低）。
- 不做 edge 文案 / handoff 内容展示（后端未透出）。

## 9. 测试

- 前端：`agent-graph-reducer` 纯函数加 native 单节点 + step 用例；组件快照（执行 tab、触发器、气泡状态、全局角标）。
- 后端：`native_runtime` 断言发出 `agent_graph`+`agent_status`+带 `agent_id` 的 tool 事件 + step 生命周期；`useChatStream` 集成测试覆盖 native 路径。
- 复用现有 `test_agent_phase*.py` / `test_debate.py` 模式。

## 10. 关键文件清单

前端：
- 改：`frontend/src/app/page.tsx`（挂统一面板 + 顶栏触发器；移除对旧 ContextPanel 的独占）
- 合并：`frontend/src/components/context/context-panel.tsx`（吸收 `multi-agent-panel.tsx` 的「执行」tab 能力，统一 Tab）
- 改：`frontend/src/components/context/execution-tab.tsx`（链路增强：mode 标签、并行分组框、节点展开）
- 改：`frontend/src/components/agents/agent-flow-graph.tsx`（并行分组框）
- 改：`frontend/src/components/agents/agent-node-card.tsx`（可展开）
- 改：`frontend/src/components/agents/agent-run-header.tsx`（mode 标签）
- 改：`frontend/src/components/agents/agent-panel-trigger.tsx`（复活 + 接入）
- 改：`frontend/src/stores/agent-run-store.ts`（响应式 selector）
- 新：`frontend/src/components/agents/agent-inline-status.tsx`（气泡内嵌状态）
- 新：`frontend/src/components/agents/agent-global-progress.tsx`（全局进度角标）
- 改：`frontend/src/components/message-bubble.tsx`（挂气泡内嵌状态）
- 改：`frontend/src/components/app-shell.tsx`（挂全局进度角标）

后端：
- 改：`backend/app/agents/runtime/native_runtime.py`（发 graph 事件 + step 生命周期 + tool 归属）
- 可能新增：`backend/app/agents/graph.py` 中 `build_single_agent_graph`（单节点图构造）
