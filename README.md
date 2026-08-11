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

## 生产部署 (Task 13)

生产拓扑是**叠加式**的：开发用 `docker-compose.yml`（源码挂载 + `--reload`），生产用独立的 `docker-compose.prod.yml`（无挂载、无 reload、迁移先行、资源限制）+ Kubernetes 清单。

### 1. 生产 Compose

```bash
cp .env.example .env.prod          # 编辑：ENV=prod、FERNET_KEY、POSTGRES_PASSWORD、ADMIN_PASSWORD…
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
```

`docker-compose.prod.yml` 与开发版的区别：

| 项 | 开发 | 生产 |
|---|---|---|
| 源码挂载 | `./backend:/app`、`./frontend:/app` | **无**（镜像内置） |
| 后端命令 | `uvicorn … --reload` | `uvicorn …`（无 reload） |
| 前端 | `next dev`（热重载） | `node server.js`（standalone 产物） |
| 迁移 | `AUTO_CREATE_TABLES=true` | `migrate` 一次性服务先跑 `alembic upgrade head` |
| Worker/Recovery | 可选 | 必启（`BACKGROUND_WORKER=durable`） |
| 资源限制 | 无 | 每 服务 `deploy.resources.limits` |
| 密钥 | `.env` | `.env.prod`（不入库；`FERNET_KEY` 必填非空） |

### 2. 迁移头强制（Migration head mandatory）

两层保障，确保落后于迁移头的镜像拿不到流量：

- **部署时**：`migrate` 服务在 API/worker/recovery 启动前执行 `alembic upgrade head`（仓库 head = `0010_artifacts`）。Kubernetes 下用 initContainer/Job 等价实现。
- **运行时**：`GET /ready`（`app/core/health.py` 的 `check_readiness`）断言 DB 的 alembic revision == 仓库 head，否则返回 **503**。这是 LB / k8s readinessProbe 的硬门。

验证脚本（隔离临时库，不碰真实数据）：

```bash
./scripts/verify_migrations.sh     # 空 库 + 增量(0009→head) 两条路径都到 0010_artifacts
```

### 3. Qdrant 客户端/服务端版本对齐

仓库曾存在版本偏移（未固定时安装了 client `1.18.0` 对 server `1.12.4`，差 6 个 minor）。Task 13 固定：

- 服务端：`qdrant/qdrant:v1.12.4`（`docker-compose.yml` + `docker-compose.prod.yml`）
- 客户端：`qdrant-client>=1.12,<1.14`（`backend/requirements.txt`，即 1.12.x 或 1.13.x，minor 偏移 ≤ 1）
- 运行时断言：`health._check_qdrant` 同时校验 **服务端 ≥ 1.10.0** 且 **客户端/服务端 minor 偏移 ≤ 1**，偏移则 `/ready` → 503，不会静默通过。一起 bump，不要单边升级。

### 4. Kubernetes 清单

```bash
kubectl apply -n mygpt -f deploy/k8s/
```

`deploy/k8s/` 包含：`config.yaml`（ConfigMap + uploads PVC）、`api.yaml`、`worker.yaml`、`recovery.yaml`、`sandbox-runner.yaml`、`network-policies.yaml`。要点：

- `securityContext.runAsNonRoot: true`（uid 1001）、`readOnlyRootFilesystem: true`、`capabilities.drop: [ALL]`、`seccompProfile: RuntimeDefault`
- `readinessProbe` → `/ready`（严格门）、`livenessProbe` → `/health`（宽松）
- 每 服务 `PodDisruptionBudget`；`NetworkPolicy` 默认拒绝 ingress+egress，仅放行最小路径
- 密钥走外部 Secret（`mygpt-secrets`，由 external-secrets / sealed-secrets / kubectl 预置），非 ConfigMap
- **sandbox-runner**：代码执行沙箱需 DinD/特权，**不能**满足非 root/只读根fs。清单用 `nodeSelector`+`tolerations` 钉到**独立隔离节点池**，并注明可用 gVisor/Kata 或外部 microVM 替代以避免特权 pod（已写入清单注释）。

> 清单为参考实现：镜像 `mygpt/backend:v1.0.0` 需先用 `backend/Dockerfile` 构建并推到你的镜像仓库；数据服务（postgres/redis/qdrant）若用托管外部服务，需把 NetworkPolicy 的 `podSelector` egress 换成对 应 ipBlock/namespaceSelector。

### 5. 备份与恢复演练

```bash
./scripts/backup.sh                          # 日常备份（cron）：pg_dump + Qdrant 快照 + uploads.tar
./scripts/restore-drill.sh ./backups/<TS>     # 恢复演练：还原到隔离容器，校验迁移头 + 校验和
./scripts/verify_migrations.sh                # 迁移头验证
```

- **Postgres**：`pg_dump -F c`（并行恢复友好）。PITR（按时间点恢复）需额外开启 WAL 归档（`archive_mode=on` + `archive_command`）+ 基础备份，演练脚本用逻辑 dump 做一致性校验。
- **Qdrant**：每集合快照 API 上传恢复。
- **对象存储**：`uploads.tar`；恢复演练做 tar 往返校验和；若备份目录带 `MANIFEST.sha256` 则按清单校验。生产建议 `STORAGE_BACKEND=minio`（对象存储自带版本化）。
- **演练目标隔离**：脚本启动一次性 postgres/qdrant 容器还原，**绝不**写真实库；校验 alembic current == `0010_artifacts` + Qdrant 集合数 + 校验和，PASS/FAIL 明确。

> 脚本沿用仓库的 `.sh`（bash）约定（与 `backup.sh`/`restore.sh` 一致）；Task 13 计划提到 `.ps1`，此处按仓库惯例统一为 `.sh`。

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
