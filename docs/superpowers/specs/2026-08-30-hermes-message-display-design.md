# Hermes 消息展示优化 — 设计文档

日期：2026-08-30 · 状态：已确认（方案 A） · 范围：后端传输层 + 前端展示层

## 背景

Hermes 模式（`backend/app/providers/hermes.py`）目前走 `/v1/chat/completions` +
`hermes.tool.progress` SSE 事件。Hermes 官方还提供更丰富的 **Runs API**
（`POST /v1/runs` → `GET /v1/runs/{id}/events`），支持 subagent 委派事件、
断线重连、`POST /v1/runs/{id}/stop` 优雅中断。

用户需求（已确认）：
1. 升级到 Runs API（能力探测，不支持自动回退 chat/completions）
2. 工具执行过程：可折叠步骤条 + 透传 Hermes 的 label/emoji + 耗时显示
3. 子代理委派展示（subagent.start/complete）
4. 会话记忆状态展示（X-Hermes-Session-Id/Key 生效徽章）
5. 运行中可停止（对接 stop 端点）
6. Hermes 专属消息头部（⚡徽章 + 状态点 + 主题色）
7. 步骤完整持久化（刷新可回看）

## 方案（A）：HermesProvider 内部升级，保持 ChatDelta 契约

### 后端

**传输层**（`hermes.py`）：
- `stream_chat()` 先 `GET /v1/capabilities`（缓存 5 分钟）。若
  `features.run_submission && run_events_sse && run_stop` 全真 → 走 Runs：
  `POST /v1/runs`（body: input=最新用户消息, session_id, instructions=系统提示
  拼接）→ 订阅 `GET /v1/runs/{run_id}/events` SSE。
- 事件映射：
  - token/assistant.delta → `ChatDelta(content=...)`
  - tool.started/completed → `meta={"hermes_tool": {...}}`（同现有契约）
  - subagent.start/complete → `meta={"hermes_subagent": {...}}`
  - run.completed/failed/cancelled → finish_reason + usage
- `stop_run()`：调 `POST /v1/runs/{id}/stop`；不支持 Runs 时 no-op（本地断流，
  行为同今天）。
- 探测失败/feature 缺失 → 原样回退现有 `_iter_sse` 路径。

**事件翻译**（`native_runtime.py` hermes_tool 分支旁）：
- `hermes_subagent` → `ev_tool_call(name="subagent", arguments={label, summary,
  duration, tokens})` / `ev_tool_result`，前端零新事件类型。

**取消链路**：native_runtime 的 cooperative-cancel 分支发现 provider 有
`stop_run` → fire-and-forget 调用（不阻塞本地取消）。

**持久化**：Hermes 模式收尾时把本流轮收集的工具步骤快照写入
`assistant_msg.metadata_["steps"]`（与前端乐观提交的 metadata.steps 同构，
刷新后由 `message-bubble.tsx` 现有代码渲染）。

### 前端

- `ResearchSteps`：
  - 工具步骤优先显示 Hermes `label`（arguments.label），无则回退 TOOL_META 映射
  - emoji 前缀（arguments.emoji）
  - 耗时 = startedAt/finishedAt 差值（如 `· 3s`）
  - 运行中展开；全部完成后默认收起（头部显示 `N 步 · 总耗时`）
  - subagent 步骤用 🧠 图标 + summary
- `MessageBubble`：Hermes 模式（metadata.hermes === true）显示专属头部：
  `⚡ Hermes Agent` 徽章 + 状态点（streaming=呼吸动画/done=绿/error=红）+
  记忆徽章（metadata.hermes_memory === true 时 `记忆已连接`）
- 主题色：紫金渐变边框 token（`--hermes-accent`），通过 data-attribute 驱动。
- 停止按钮：复用现有 chat.stop（后端 cancel 已触发 stop_run）。

## 测试

- 后端：`test_hermes_provider.py` 增加 Runs 路径用例（capabilities 探测、
  事件映射、回退）；native_runtime 的 subagent 翻译断言。
- 前端：`npm run build` + 现有测试；ResearchSteps 快照更新。

## 非目标

- /v1/responses API、Jobs API、浏览器扩展控制
- Hermes 的 thinking/reasoning 内容展示（Runs 事件流暂无此数据）
