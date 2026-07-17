# AI 对话平台 (AI Chat Platform)

私有化、可扩展的 AI 对话与知识库平台，对标 ChatGPT / 豆包 / Kimi。支持多轮流式对话、知识库 RAG 问答、文件上传、工具调用扩展、用户权限、管理后台、Docker 一键部署。模型通过 **OpenAI-compatible API** 接入（vLLM / Ollama / DeepSeek / Qwen / OpenAI / 你的自研模型）。

> 架构与设计见 `docs/superpowers/specs/2026-07-09-ai-chat-platform-mvp-design.md`。

---

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | Next.js 14 (App Router) · TypeScript · Tailwind · shadcn/ui · react-markdown |
| 后端 | FastAPI · Pydantic v2 · SQLAlchemy 2.0 (async) · Alembic |
| 数据库 | PostgreSQL 16 |
| 向量库 | Qdrant（默认，pgvector 可插拔） |
| 缓存 | Redis |
| 鉴权 | JWT(access+refresh) + argon2 |

## 核心原则

- 所有模型调用走 `app/providers/`（OpenAICompatible / Mock），业务代码不直连模型。
- 所有知识库检索走 `app/rag/RagService`。
- 所有工具调用走 `app/tools/ToolRegistry`。
- 前端永不直连模型 API；API Key 永不进浏览器（加密入库 + 掩码回显）。

---

## 快速开始（Docker 一键）

```bash
cp .env.example .env            # 按需修改（至少改 JWT_SECRET / FERNET_KEY / 模型配置）
docker compose up -d
```

- 前端：http://localhost:3000
- 后端 API 文档：http://localhost:8000/docs
- 默认管理员（首次启动自动创建）：`admin@example.com` / `changeme123`（在 `.env` 里改）

首次登录后到 **设置 → 模型** 配置你的模型（Base URL / API Key / 模型名），点"测试连接"。未配置模型时，可使用内置 `mock` 提供者体验完整流式对话。

---

## 本地开发（非 Docker）

后端：
```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# 本地用 .env 里指向 localhost 的 DATABASE_URL/REDIS_URL/QDRANT_URL
uvicorn app.main:app --reload --port 8000
```

前端：
```bash
cd frontend
corepack enable && corepack prepare pnpm@9.12.0 --activate
pnpm install
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 pnpm dev
```

---

## 接入你自己的模型

在 **设置 → 模型 → 新建**，或通过环境变量配置：

```env
MODEL_PROVIDER=openai-compatible
MODEL_API_BASE_URL=http://localhost:8000/v1   # 你的 vLLM / SGLang / Ollama / 云端 OpenAI 兼容端点
MODEL_API_KEY=your-api-key
MODEL_NAME=my-model
EMBEDDING_API_BASE_URL=http://localhost:8000/v1
EMBEDDING_API_KEY=your-api-key
EMBEDDING_MODEL_NAME=my-embedding-model
```

要求模型端点兼容 OpenAI Chat Completions（`POST /v1/chat/completions`，支持 `stream:true`），embedding 兼容 `POST /v1/embeddings`。

### 常见模型服务

| 服务 | Base URL | 备注 |
|---|---|---|
| vLLM / SGLang | `http://<host>:<port>/v1` | 本地部署，OpenAI 兼容 |
| Ollama | `http://<host>:11434/v1` | Ollama 提供 OpenAI 兼容层 |
| OpenAI | `https://api.openai.com/v1` | 官方 |
| DeepSeek | `https://api.deepseek.com/v1` | OpenAI 兼容 |
| 通义千问 (DashScope 兼容模式) | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容 |

> **注意 embedding 维度**：在 `.env` 中把 `QDRANT_EMBEDDING_DIM` 设成与你 embedding 模型输出一致（默认 1024），否则入库/检索会失败。

---

## 数据库迁移

开发模式 `AUTO_CREATE_TABLES=true` 会按模型自动建表（便于快速起步）。生产请使用 Alembic：

```bash
cd backend
alembic revision --autogenerate -m "init"
alembic upgrade head
```

---

## 项目结构

```
ai-chat-platform/
  frontend/        Next.js (pnpm)
    src/app/       pages (login, chat, settings, knowledge-bases, admin)
    src/components/
    src/hooks/     useChatStream, useAuth, ...
    src/lib/       api client, types, utils
  backend/
    app/
      api/         routers: auth, conversations, chat, models, knowledge_bases,
                    documents, retrieval, tools, admin
      core/        config, security, deps, logging
      models/      SQLAlchemy ORM
      schemas/     Pydantic DTOs
      services/    ChatService, AuthService, ConversationService, RagService, ToolService
      providers/   ModelProvider: openai_compatible, mock, registry
      rag/         parser, splitter, embedder, qdrant store, retriever, reranker
      tools/       registry, builtin tools, executor, agent loop
    migrations/    Alembic
    tests/
  docker-compose.yml
  .env.example
  README.md
  docs/
```

---

## 生产部署建议

1. 生产 `.env`：`ENV=prod`、强随机的 `JWT_SECRET` 与 `FERNET_KEY`、关闭 `AUTO_CREATE_TABLES`，用 Alembic 迁移。
2. 前端去掉 `--reload` / `pnpm dev`，改用构建产物（`output: standalone`，见 Dockerfile 的 prod 阶段）。
3. 后端用 `gunicorn -k uvicorn.workers.UvicornWorker` 多进程，前置 Nginx 做 TLS 终止与静态资源。
4. 启用 MinIO/S3 替代本地存储（`STORAGE_BACKEND=minio`）。
5. 关闭注册（环境变量控制）或改为邀请制。
6. 定期备份 PostgreSQL（`pg_dump`）与 Qdrant（快照）。

---

## 常见问题排查

- **前端打不开 / 接口 401**：检查 `.env` 的 `BACKEND_CORS_ORIGINS` 是否包含前端地址；token 过期会自动用 refresh cookie 续期，refresh 也失败则需重新登录。
- **模型测试失败**：确认 `MODEL_API_BASE_URL` 末尾通常带 `/v1`；`MODEL_API_KEY` 有效；服务可达（容器内是 `http://<service>:port`）。
- **embedding 报维度错误**：把 `QDRANT_EMBEDDING_DIM` 改为你的 embedding 模型输出维度后，删除并重建知识库集合。
- **上传文件失败**：检查 `ALLOWED_UPLOAD_EXT` 与 `MAX_UPLOAD_MB`；`STORAGE_DIR` 卷可写。
- **Qdrant 健康检查不通过**：等待首次启动完成；或临时去掉 depends_on 条件。

---

## 状态

P0（对话）/ P1（知识库 RAG）/ P2（工具调用）/ P3（管理后台）已实现端到端骨架并在同一架构上。详见设计文档与代码。
