# AI 对话平台 — 总体架构 + MVP(P0) 设计

- 状态：待用户评审
- 日期：2026-07-09
- 范围：总体架构（所有阶段共用）+ 第一个子项目 MVP(P0) 的详细设计
- 后续阶段（P1 RAG / P2 工具 / P3 管理后台）各走独立的 设计→计划→构建 周期，沿用本文架构

---

## 1. 目标与原则

构建私有化、类 ChatGPT/豆包的 AI 对话平台：网页多轮对话、知识库 RAG 问答、文件上传、工具调用扩展、用户与权限、Docker 一键部署。模型通过 **OpenAI-compatible API** 接入（vLLM / Ollama / DeepSeek / Qwen / OpenAI 等）。

**不可动摇的实现原则：**

1. 所有模型调用必走 `providers/`，业务代码不直连模型 API。
2. 所有知识库检索必走 `rag/RagService`。
3. 所有工具调用必走 `tools/ToolRegistry`。
4. 前端绝不直连模型 API；API Key 永不进浏览器。
5. 先 MVP 可跑，但架构预留扩展点（Provider / VectorStore / Storage / Tool / Parser 全部接口化）。

---

## 2. 系统架构

```
浏览器(Next.js :3000)
   │ REST(JSON) + SSE(流式 token), 带 JWT
   ▼
FastAPI 后端(:8000)
   api/ ── services/ ── { providers/  rag/  tools/ } ── core/
   │
   ├── PostgreSQL(关系数据 + 预留 pgvector)
   ├── Redis(限流 / refresh 黑名单 / 后台任务)
   ├── Qdrant(向量库, 默认)
   └── 你的模型 /v1/chat/completions · /v1/embeddings
```

模块边界（单一职责、接口明确、可独立测试）：

| 模块 | 职责 |
|---|---|
| `core/` | 配置(pydantic-settings)、安全(JWT/argon2)、依赖注入(get_current_user)、日志、中间件 |
| `models/` | SQLAlchemy ORM：User / Conversation / Message / ModelConfig（+ P1: KnowledgeBase / Document / DocumentChunk / ToolCall） |
| `schemas/` | Pydantic 请求/响应 DTO，与前端类型同形 |
| `providers/` | `ModelProvider` 抽象 + `OpenAICompatibleProvider` + `MockProvider`；chat / streamChat / embeddings |
| `services/` | `ChatService`（编排：鉴权→取历史→可选 RAG→流式→落库）、`AuthService`、`ConversationService` |
| `rag/` | (P1) Parser / Splitter / Embedder / VectorStore / Retriever / Reranker / RagService |
| `tools/` | (P2) `ToolRegistry` / `BaseTool` / 内置工具 / 执行循环 |
| `api/` | 路由：auth / conversations / chat(SSE) / models / knowledge_bases / documents / retrieval / tools / admin |

---

## 3. 技术栈（已锁定）

| 层 | 选型 |
|---|---|
| 前端 | Next.js 14 App Router · TypeScript · Tailwind · shadcn/ui · react-markdown(+remark-gfm,+rehype-highlight) |
| 流式接收 | `fetch` + `ReadableStream`（SSE 走 POST 且需带 Authorization 头，EventSource 不适用） |
| 后端 | FastAPI · Pydantic v2 · SQLAlchemy 2.0(async) · Alembic · asyncpg · python-jose(JWT) · passlib[argon2] |
| 数据库 | PostgreSQL 16 |
| 向量库 | **Qdrant（默认）**；pgvector 作为 `VectorStore` 可插拔实现 |
| 缓存/队列 | Redis |
| 文件解析 | pdfplumber / python-docx / openpyxl / pandas / markdown（Parser 适配器封装；P1 起用） |
| 对象存储 | 本地文件系统（MVP）+ `Storage` 适配器 → 后续 MinIO/S3 |
| 包管理 | 前端 pnpm，后端 uv，docker-compose 编排 |

---

## 4. 关键决策（已与用户确认）

| 决策 | 选择 | 理由 |
|---|---|---|
| 前端 | Next.js App Router，直连 FastAPI（无 BFF） | 生产级、可扩展、无多余层 |
| 向量库 | Qdrant 优先（pgvector 可插拔） | 用户选定；专用向量库、规模与性能更优 |
| 鉴权 | JWT access(短期) + refresh(httpOnly cookie) | 无状态、易扩展，与 `JWT_SECRET` 一致 |
| 模型可运行性 | 内置 `MockProvider`，零外部依赖即可跑通流式 | 交付时可实测核心链路 |
| 存储 | 本地 FS + 适配器 | 快速落地，后续可换 MinIO |

---

## 5. 阶段规划

- **P0（本次设计范围）MVP**：鉴权 + 多轮流式对话 + 会话历史 + 模型配置。端到端可跑。
- **P1 RAG / 知识库**：文件上传、解析、切分、embedding、Qdrant 入库、检索、RAG 问答、引用来源。
- **P2 工具调用 / Agent**：ToolRegistry、function calling 循环、多轮工具、内置工具（web_search / python / db_query 等）。
- **P3 管理后台**：用户/模型/Key/知识库管理、系统 Prompt、用量统计、日志、权限。

---

## 6. MVP(P0) 详细设计

### 6.1 范围

**纳入 P0：**

- 认证：register / login / me / logout；JWT access+refresh；argon2 密码。
- 会话：创建 / 列表 / 详情 / 删除；按用户隔离。
- 消息：user/assistant 持久化；多轮上下文。
- 流式对话：SSE 下发 token；停止生成；重新生成；复制。
- 模型配置：CRUD + 连通性 test；API Key 不明文回前端。
- Provider 层：`MockProvider` + `OpenAICompatibleProvider`（chat / streamChat / embeddings，预留 tools）。
- 前端：登录页、聊天页（左侧会话列表 + 中间消息流 + 底部输入框 + 模型选择 + 设置入口）、设置/模型配置页。

**明确不在 P0（预留接口/表结构但暂不实现）：** 知识库、文件上传、向量检索、工具调用执行、管理后台 UI、用量统计。（P1+）

### 6.2 数据模型（P0 表）

```sql
users
  id UUID PK
  email TEXT UNIQUE NOT NULL
  username TEXT UNIQUE NOT NULL
  password_hash TEXT NOT NULL          -- argon2
  role TEXT NOT NULL DEFAULT 'user'     -- user | admin
  created_at, updated_at TIMESTAMPTZ

conversations
  id UUID PK
  user_id UUID FK -> users.id
  title TEXT NOT NULL DEFAULT '新对话'
  model_id UUID FK -> model_configs.id (nullable)
  knowledge_base_id UUID NULL           -- P1 预留
  created_at, updated_at TIMESTAMPTZ

messages
  id UUID PK
  conversation_id UUID FK -> conversations.id
  role TEXT NOT NULL                    -- system | user | assistant | tool
  content TEXT NOT NULL
  metadata JSONB DEFAULT '{}'           -- 引用/工具/模型名/耗时 等
  created_at TIMESTAMPTZ

model_configs
  id UUID PK
  user_id UUID FK -> users.id           -- 归属用户；系统级可 user_id NULL
  name TEXT NOT NULL
  provider TEXT NOT NULL                -- openai-compatible | mock
  api_base_url TEXT NOT NULL
  api_key_encrypted TEXT                -- 加密存储，回前端时 mask
  model_name TEXT NOT NULL
  embedding_model_name TEXT NULL        -- P1 用
  supports_stream BOOL DEFAULT TRUE
  supports_tools BOOL DEFAULT FALSE
  temperature REAL DEFAULT 0.7
  top_p REAL DEFAULT 1.0
  max_context_tokens INT DEFAULT 8192
  max_tokens INT DEFAULT 1024
  created_at, updated_at TIMESTAMPTZ
```

迁移用 Alembic 管理；API Key 用 `FERNET_KEY`（env）对称加密后存库，返回前端时只给 `sk-****1234` 形式的掩码。

### 6.3 API 契约（P0）

```
POST   /api/auth/register      {email, username, password}            -> {user}
POST   /api/auth/login         {email, password}                      -> {access_token, user}; Set-Cookie refresh
GET    /api/auth/me                                                   -> {user}
POST   /api/auth/refresh                                              -> {access_token}; 轮转 refresh cookie
POST   /api/auth/logout                                               -> 204; 清 refresh cookie

GET    /api/conversations?limit&cursor                                -> {items, next_cursor}
POST   /api/conversations     {title?, model_id?}                     -> {conversation}
GET    /api/conversations/{id}                                        -> {conversation, messages[]}
DELETE /api/conversations/{id}                                        -> 204   (校验归属)
POST   /api/conversations/{id}/regenerate                             -> SSE(同 /chat/stream 语义)

POST   /api/chat/stream       {conversation_id, model_id?, content}   -> SSE

GET    /api/models                                                    -> ModelConfig[](key 掩码)
POST   /api/models            {name, provider, api_base_url, api_key, model_name, ...}
PUT    /api/models/{id}                                               -> ModelConfig
DELETE /api/models/{id}                                              -> 204
POST   /api/models/{id}/test                                          -> {ok, latency_ms, sample?}
```

所有 `/api/*` 除 register/login 外需 `Authorization: Bearer <access>`；资源类接口校验 `resource.user_id == current_user.id`（admin 例外）。

### 6.4 Provider 层设计

```python
class ModelProvider(Protocol):
    async def chat(self, messages, *, model, tools=None, **opts) -> ChatResult: ...
    async def stream_chat(self, messages, *, model, tools=None, **opts) -> AsyncIterator[ChatDelta]: ...
    async def embeddings(self, texts, *, model) -> list[list[float]]: ...

class OpenAICompatibleProvider(ModelProvider):  # 自定义 base_url / api_key / model / temperature / top_p / max_tokens
    # 非流式 / 流式 / 超时 / 重试(指数退避) / API Key 注入 / tools 预留
    ...

class MockProvider(ModelProvider):  # 回显 + 假流式 + 假 embedding，零依赖，用于演示与测试
    ...
```

`providers/registry`：按 `model_configs.provider` 取实现，注入该配置的 base_url/key/model。tools 字段透传，P2 再接执行循环。

### 6.5 SSE 流式协议

`POST /api/chat/stream` 响应 `Content-Type: text/event-stream`，逐行 `event: <t>\ndata: <json>\n\n`：

```
event: meta      data: {"message_id": "..."}
event: token     data: {"delta": "你"}            # 反复下发
event: tool_call data: {"id","name","arguments"}  # P2
event: tool_result data: {...}                    # P2
event: done      data: {"message_id","finish_reason"}
event: error     data: {"code","message"}
```

- 停止生成：前端 `AbortController.abort()` → 后端通过 `await request.is_disconnected()` 探测 → 已生成部分落库为 assistant 消息。
- 重新生成：删除最后一条 assistant 消息 → 以原 user 消息重跑 `/chat/stream`。

### 6.6 安全与权限（P0 基线）

- 密码 argon2 哈希；JWT access 15min（内存/Authorization 头），refresh 7d（httpOnly+Secure+SameSite=Lax cookie），refresh 轮转 + Redis 黑名单。
- API Key 加密入库、掩码回前端。
- 资源归属校验（会话/模型只能访问自己的）。
- 接口鉴权中间件；基础输入校验（长度上限、拒绝畸形 JSON）。
- 防 prompt injection 基线：system prompt 固化优先级、对用户输入做长度/字符清洗、不在系统 prompt 内直接拼接未过滤内容（P1 RAG 时进一步加固）。
- 关键操作（登录/模型增删/会话删除）写日志。

### 6.7 前端页面与状态（P0）

- **登录页** `/login`：邮箱+密码；成功后存 access(内存) + refresh(cookie 自动)。
- **聊天页** `/`：左侧（新建对话 / 会话列表 / 设置入口）；中间（消息流，user/assistant 气泡，Markdown+代码高亮，生成中状态，停止/重新生成/复制）；底部（输入框 + 发送 + 模型选择器）。
- **设置页** `/settings/models`：模型配置 CRUD + 测试连接。
- 状态管理：React Query（服务端状态：会话/消息/模型）+ Zustand（UI 状态：当前会话、流式 buffer）。流式用自定义 `useChatStream` hook（fetch ReadableStream 解析 SSE）。
- auth：Axios/fetch 拦截器自动带 access；401 时用 refresh 静默续期一次。

### 6.8 错误处理与日志

- 后端：全局异常处理器 → 统一 `{code, message}`；区分 400(校验)/401(未认证)/403(越权)/404(不存在)/429(限流)/500。
- Provider 调用：超时 + 重试(可配置次数) + 降级到 error 事件。
- 日志：structlog，请求级（method/path/status/latency/user_id）+ 业务级（模型调用耗时/token 数）。

### 6.9 测试策略

- 后端：pytest。
  - 单元：Provider（用 MockProvider + httpx Mock 对 OpenAICompatible）、JWT/argon2、会话归属校验。
  - 接口：FastAPI TestClient 跑 auth/会话/模型 CRUD。
  - SSE：TestClient 流式断言 token/done 事件序列。
- 前端：Vitest + React Testing Library 测 `useChatStream` SSE 解析与消息渲染。
- 连通性：`POST /api/models/{id}/test` 对真实模型做一次 1-token 健康检查（可用时）。
- 部署：`docker compose up -d` 后 curl 健康检查 + 浏览器打开 :3000 冒烟。

### 6.10 部署（P0 docker-compose）

```
frontend(3000)  backend(8000)  postgres(5432)  redis(6379)  qdrant(6333)
.env.example 提供全部变量；docker compose up -d 后访问 http://localhost:3000
```

---

## 7. 目录结构（monorepo 平铺）

```
ai-chat-platform/                      # 即 D:\Gitee\MyGPT
  frontend/        (Next.js, pnpm)
  backend/
    app/
      api/ core/ models/ schemas/ services/ providers/ rag/ tools/ utils/
    migrations/  (Alembic)
    tests/
  docker/          (各服务 Dockerfile 片段)
  docker-compose.yml
  .env.example
  README.md
  docs/            (含本设计文档)
```

---

## 8. 待确认 / 后续

- 实测链路：先以 MockProvider 跑通并自测；真实模型端点（如 vLLM :8000）可用时再做 OpenAICompatibleProvider 冒烟。构建前确认你是否已有可用的模型端点。
- git：当前目录非 git 仓库；按你的约定“仅在你要求时提交”，暂不 init/commit，需要时我再做。
- P1 起将启用：rag/ 模块、Qdrant 索引、文件上传、引用来源展示。

---

## 9. P0 验收标准

1. `docker compose up -d` 成功，访问 :3000 看到登录页。
2. 注册→登录→创建会话→（用 MockProvider 或真实模型）流式多轮对话，token 实时显示，Markdown/代码块正常渲染。
3. 停止生成、重新生成、复制均可工作；刷新后会话与消息仍在。
4. 会话/模型按用户隔离，越权访问返回 403/404。
5. 模型配置页可增删改 + 测试连接；API Key 不明文回显。
6. 后端接口测试 + 前端核心流程测试通过。
