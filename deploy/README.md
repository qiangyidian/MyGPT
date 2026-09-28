# 自动部署（服务器侧组件）

本目录包含 mychat.qiangi.top 的自动部署组件。镜像部署流程由
`.github/workflows/ci.yml`、`.github/workflows/deploy.yml` 与服务器 systemd timer 配合完成。

## 架构

```
push main → GitHub Actions：代码检查 + PostgreSQL 迁移门禁
         → 构建后端/前端镜像 → 推送 GHCR（sha-<commit>）
         → 全部 CI 通过后推送对应 SHA 到 deploy/SIGNAL
         → 服务器 timer 轮询到信号
         → 拉取该 SHA 镜像 → 数据库迁移
         → 暂停旧 systemd 服务 → 启动 Docker Compose 服务
         → /ready + 前端健康检查 → 失败恢复上一镜像或旧 systemd 服务
```

CI 产物按 Git SHA 固定版本，服务器不再安装依赖或编译前端。敏感配置
（`DATABASE_URL`、API key 等）只从服务器上权限为 `600` 的 `.env` 读取，仓库不含密钥。
服务器现有 PostgreSQL、Redis、Qdrant 和上传目录继续由宿主机提供；
`docker-compose.server.yml` 只运行应用服务，避免切换到另一套空数据库。

## 在新服务器上安装

```bash
# 前置：Docker Engine + `docker compose` 插件、`/root/MyGPT` clone 和生产 `.env`。
# .env 要包含正确的数据服务 URL、STORAGE_DIR、生产密钥以及独立沙箱需要的 DOCKER_HOST。
# GHCR 镜像需允许服务器匿名拉取；仓库公开时请将首次发布的镜像包设为 Public。
# 从仓库根目录安装：
sudo cp deploy/mychat-deploy.sh /usr/local/bin/ && sudo chmod +x /usr/local/bin/mychat-deploy.sh
sudo cp deploy/mychat-deploy.service deploy/mychat-deploy.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mychat-deploy.timer
```

从旧源码部署切换时，先确认生产 `.env` 中 `REDEEM_CODE_PEPPER` 已配置为独立随机值；
部署器会预检它，未配置时停止并保留当前 systemd 服务。只有镜像拉取和数据库迁移成功后
才会切换流量。容器健康后会禁用旧应用 systemd units，避免主机重启时端口冲突；
首次容器启动若健康检查失败，部署器会重新启用并启动原 systemd 服务。

沙箱仍应连接独立 Docker daemon：Compose 不挂载应用机的 `/var/run/docker.sock`。
`SANDBOX_SCRATCH_ROOT`（部署默认 `/opt/mychat-data/sandbox`）需在 sandbox daemon 一侧
映射到同一宿主路径，否则代码执行工具无法挂载工作区。

## 部署清单（一台新服务器要装什么）

| 单元 | 作用 | 装法 |
|---|---|---|
| `mychat-deploy.{sh,service,timer}` | 每 2 分钟检查信号、拉镜像、迁移、健康检查和回滚 | 见上面「在新服务器上安装」 |
| Docker Compose `backend/frontend/worker/recovery` | 运行 CI 构建的固定 SHA 镜像 | 由部署器启动；切换后旧 systemd 服务停止 |
| `mychat-backup.{service,timer}` | 每天 03:00 全量备份 + 异地推送 | 见下面「备份与恢复」 |
| `mychat-backup-alert.service` | 备份 job 失败的兜底告警（`OnFailure=`） | 跟备份单元一起装 |

RPO / RTO 的实际数值、恢复步骤、异地同步的配置与失败语义、以及"什么情况下不该用这份
备份"的检查单，都在 **[../docs/backup-restore.md](../docs/backup-restore.md)**。
事故里请读那份，不要读本文件。

## 备份与恢复

```bash
# 1. 备份凭据与异地配置（chmod 600，仓库零密钥）
sudo mkdir -p /etc/mychat && sudo tee /etc/mychat/backup.env >/dev/null <<'EOF'
BACKUP_RCLONE_REMOTE=mygpt-offsite
BACKUP_RCLONE_PREFIX=backups/mychat-prod
RETAIN_REMOTE_DAYS=30
BACKUP_ALERT_URL=https://<企业微信/钉钉 robot webhook>
EOF
sudo chmod 600 /etc/mychat/backup.env

# 2. 远端必须是 rclone 的 crypt 类型（备份含用户上传原文与 API key 密文）
rclone config   # type=crypt 包住真实 s3/sftp 后端；否则 sync-offsite.sh 会直接失败

# 3. 安装三个单元并启用排期
sudo chmod +x /root/MyGPT/scripts/*.sh    # 仓库里 .sh 的 git 模式是 100644，没有可执行位
sudo cp mychat-backup.service mychat-backup-alert.service mychat-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mychat-backup.timer

# 4. 立刻手跑一次，确认它真的能出产物并且推上异地（不要等明天 03:00）
sudo systemctl start mychat-backup.service
journalctl -u mychat-backup.service -f
```

注意：

- `backup.sh` 从仓库 `.env` 读 `STORAGE_DIR` / `DATABASE_URL` / `QDRANT_URL`，
  单元里**不再**硬编码 `STORAGE_DIR=/data/uploads`（那个值是容器内路径，宿主上不存在，
  会让附件被静默漏备）。
- 本目录的 `monitoring/` 是 Prometheus + Alertmanager（compose 的 `monitoring` profile）。
  它**抓不到 systemd 一次性任务**，所以备份失败不会变成一条告警规则；备份的告警通道是
  `BACKUP_ALERT_URL` + journal + `OnFailure=mychat-backup-alert.service`。
- 判活用 `systemctl --failed | grep mychat-backup`，不要用 `list-timers`
  （后者只证明排上了期，不证明上一次成功）。
- 迁移 head 一律以 `ls backend/migrations/versions` / `alembic heads` 现算为准，
  本文件与部署脚本都不钉死它（钉死过一次，导致带迁移的部署 `/ready` 全红）。

## 日常操作

```bash
systemctl start mychat-deploy.service   # 手动触发一次部署
journalctl -t mychat-deploy -f          # 盯部署日志
IMAGE_TAG="sha-$(cat /var/lib/mychat-deploy/last_deployed_commit)" \
docker compose --project-name mychat --env-file /root/MyGPT/.env \
  -f /opt/mychat-deploy/releases/<commit-sha>/docker-compose.server.yml ps
```

## 安全特性

- **CI 门禁**：所有 CI job 成功后才写 deploy 信号；不支持手动绕过 CI
- **不可变镜像**：部署以 `sha-<commit>` 精确定位前后端镜像，不使用浮动 `latest`
- **服务器不构建**：依赖安装和前端生产构建都在 CI 完成
- **健康检查 + 自动回滚**：`/ready` 或前端不可达时恢复上一 SHA；首次失败则恢复 systemd 服务
- **迁移门禁**：先拉取镜像并执行 `alembic upgrade head`；失败不会停止旧服务
- **浏览器 API 地址**：CI 构建镜像时注入 `NEXT_PUBLIC_API_BASE_URL=https://mychat.qiangi.top`
- **数据库连接预算**：API 默认最多 8 × 8 = 64 个池连接；worker 与 recovery 各最多 3 + 1 个。HPA 上限、所有 API/后台副本和迁移任务的连接上界必须低于 PostgreSQL `max_connections`，并预留管理连接。
- **密钥隔离**：所有凭据来自服务器本地 `.env`，仓库零密钥

## 紧急操作

```bash
# 回滚请发布 revert commit：完整 CI、固定版本镜像、信号门禁都会再次执行。
```
