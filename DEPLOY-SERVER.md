# MyChat 生产部署说明（真实服务器）

域名：**https://mychat.qiangi.top**（nginx + Let's Encrypt）
前端：`127.0.0.1:5003`（next start）　后端：`127.0.0.1:8003`（uvicorn）

## systemd 服务（全部开机自启）

| 服务 | 说明 |
|---|---|
| `mychat-backend.service` | FastAPI/uvicorn @8003，venv `/opt/mychat-venv`（Python 3.12） |
| `mychat-frontend.service` | Next.js @5003，生产构建（已嵌入 `NEXT_PUBLIC_API_BASE_URL=https://mychat.qiangi.top`） |
| `qdrant.service` | Qdrant 1.12.4 二进制 @6333，数据在 `/opt/qdrant/storage` |

（`mychat-certbot-watch.service` 是部署时等 DNS 用的一次性 watcher，证书签发完成后已停止并 disable。）

依赖的本机服务：`postgresql@14-main`（库 `ai_chat`，用户 `mychat`）、`redis-server`（db 3）。

## 配置文件

- `.env` — 生产环境变量（已 chmod 600）。包含数据库密码、JWT_SECRET、FERNET_KEY、
  管理员初始密码（`admin` / 见 .env，首次登录后请改）。
- `/etc/nginx/sites-available/mychat` — 完整 https 配置（证书签发后由 watcher 自动启用）。
- `/etc/nginx/sites-available/mychat-bootstrap` — 过渡用 80 端口配置（ACME challenge + 跳转）。
- `/usr/local/bin/mychat-certbot-watch.sh` — DNS 等待 + certbot 签发 + nginx 切换。

## DNS 与证书（已完成 ✅ 2026-08-29）

`mychat.qiangi.top A 103.212.187.98` 已生效，Let's Encrypt 证书已签发
（到期 2026-11-27，certbot.timer 自动续期，续期后 reload-nginx.sh 自动 reload nginx）。
https 全链路已验证：首页/登录页 200、http→https 301、`/api/chat/stream` SSE 流式正常。

## 运维命令

```bash
systemctl restart mychat-backend     # 改了后端代码后
systemctl restart mychat-frontend    # 改了前端代码后（需先重新 build，见下）
journalctl -u mychat-backend -f      # 看日志
curl http://127.0.0.1:8003/health    # 存活探针（db/redis/qdrant）
curl http://127.0.0.1:8003/ready     # 就绪探针（严格）
```

### 重新构建前端（改前端代码后）

```bash
cd /root/MyGPT/frontend
NEXT_PUBLIC_API_BASE_URL=https://mychat.qiangi.top npm run build
systemctl restart mychat-frontend
```

### 换成真实模型

当前用内置 Mock provider（可完整走通流程，但回复是回显）。
在管理后台（admin 登录 → 模型管理）添加 OpenAI 兼容端点即可，或改 `.env` 的
`MODEL_*` / `EMBEDDING_*` 后 `systemctl restart mychat-backend`。

### 数据备份

- Postgres: `pg_dump -h 127.0.0.1 -U mychat ai_chat`（密码在 .env）
- 上传文件：`/opt/mychat-data/uploads`
- Qdrant：`/opt/qdrant/storage`

## 部署中修复的 Bug

`backend/app/services/document_service.py`：模块级 `async def delete()` 遮蔽了
SQLAlchemy 的 `delete` import，导致 `_clear_existing` 在文档索引时必然报
`TypeError: delete() missing 1 required positional argument`，所有文档索引失败。
已改为在函数内显式 `from sqlalchemy import delete as _sa_delete`。
（发现方式：端到端上传文档验证；修复后 1024 个后端测试全部通过。）
