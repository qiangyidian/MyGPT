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

## 日常操作

```bash
systemctl start mychat-deploy.service   # 手动触发一次部署
journalctl -t mychat-deploy -f          # 盯部署日志
```

## 安全特性

- **CI 门禁**：main 有新 commit 但没有对应 deploy 信号（CI 未绿）→ 不部署
- **脏工作区保护**：服务器上有未提交改动 → 拒绝部署（不吞本地修改）
- **健康检查 + 自动回滚**：部署后 `/ready` 不可达 → 回滚到上一 commit 重新构建
- **依赖幂等**：`requirements.txt` 的 sha256 没变就跳过 pip install
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
