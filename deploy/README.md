# 自动部署（服务器侧组件）

本目录是 mychat.qiangi.top 服务器上实际运行的自动部署组件快照，
配合 `.github/workflows/deploy.yml` 使用。

## 架构

```
其他机器 push main → GitHub Actions CI（测试/构建门禁）
                        ↓ 全绿
                  推 SIGNAL 到 deploy 分支
                        ↓
服务器 mychat-deploy.timer（每 2 分钟）
    mychat-deploy.sh：
      fetch → CI 信号门禁 → 依赖变更检测 → 前端构建
      → alembic 迁移 → 重启服务 → /ready 健康检查 → 失败自动回滚
```

敏感配置（`DATABASE_URL`、API key 等）一律从服务器上 `chmod 600` 的
`.env` 读取（`set -a; . .env; set +a`），本仓库不含任何密钥。

## 在新服务器上安装

```bash
# 0. 前置：代码 clone 到 /root/MyGPT、python3.12 venv 在 /opt/mychat-venv、
#    生产 .env 就位（参照 DEPLOY-SERVER.md）
#
# 1. 按机器实际情况修改脚本顶部的
#    REPO / VENV / BACKEND_READY_URL / ENV_FILE / 前端端口
#
# 2. 安装
sudo cp mychat-deploy.sh /usr/local/bin/ && sudo chmod +x /usr/local/bin/mychat-deploy.sh
sudo cp mychat-deploy.service mychat-deploy.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mychat-deploy.timer
```

## 部署清单（一台新服务器要装什么）

| 单元 | 作用 | 装法 |
|---|---|---|
| `mychat-deploy.{sh,service,timer}` | 每 2 分钟拉信号并自动部署 + 回滚 | 见上面「在新服务器上安装」 |
| `mychat-worker.service` / `mychat-recovery.service` | 持久 run 队列的消费者 / 租约过期接管 | `EnvironmentFile=/root/MyGPT/.env`，直接 enable |
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
```

## 安全特性

- **CI 门禁**：main 有新 commit 但没有对应 deploy 信号（CI 未绿）→ 不部署
- **脏工作区保护**：服务器上有未提交改动 → 拒绝部署（不吞本地修改）
- **健康检查 + 自动回滚**：部署后 `/ready` 不可达 → 回滚到上一 commit 重新构建
- **依赖幂等**：`backend/requirements.lock.txt` 的 sha256 没变就跳过安装；锁文件带哈希，变更后由 CI 校验
- **浏览器 API 地址**：`NEXT_PUBLIC_API_BASE_URL` 必须在 `docker-compose.prod.yml` 构建 frontend 镜像时提供；Next.js 在 build 阶段内联此值，运行时 environment 不能改写浏览器 bundle
- **数据库连接预算**：API 默认最多 8 × 8 = 64 个池连接；worker 与 recovery 各最多 3 + 1 个。HPA 上限、所有 API/后台副本和迁移任务的连接上界必须低于 PostgreSQL `max_connections`，并预留管理连接。
- **密钥隔离**：所有凭据来自服务器本地 `.env`，仓库零密钥

## 紧急操作

```bash
# 跳过 CI 门禁直接部署：GitHub → Actions → Deploy Signal → Run workflow
#   （workflow_dispatch 路径，用于 CI 挂了但需要紧急上线）

# 回滚到任意历史版本
git -C /root/MyGPT reset --hard <commit>
cd /root/MyGPT/frontend && \
  NEXT_PUBLIC_API_BASE_URL=https://mychat.qiangi.top npm run build
sudo systemctl restart mychat-backend mychat-frontend
```
