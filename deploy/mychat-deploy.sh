#!/usr/bin/env bash
# =============================================================================
# MyChat 自动部署脚本 — 由 mychat-poll-deploy.service/timer 或手动调用。
# 流程：安全上下文检查 → 拉取 main → 依赖变更检测 → 前端构建 →
#       数据库迁移 → 服务重启 → 健康验证（失败自动回滚到部署前 commit）。
# =============================================================================
set -euo pipefail

REPO=/root/MyGPT
BRANCH=main
VENV=/opt/mychat-venv
LOG_TAG="mychat-deploy"
BACKEND_READY_URL="http://127.0.0.1:8003/ready"
STATE_FILE=/var/lib/mychat-deploy/last_deployed_commit
HEALTH_RETRIES=15
# 敏感配置（DATABASE_URL 等）从已 chmod 600 的 .env 读取，绝不入库
ENV_FILE="$REPO/.env"

log() { echo "[$(date '+%F %T')] $*" | logger -t "$LOG_TAG" -s 2>/dev/null || echo "[$(date '+%F %T')] $*"; }

mkdir -p /var/lib/mychat-deploy

cd "$REPO"

# ---- 1. 安全上下文：绝不在脏工作区上部署（本地实验改动会被吞掉） ----
if ! git diff --quiet || ! git diff --cached --quiet; then
    log "REFUSE: 工作区有未提交改动，跳过部署（避免吞掉本地修改）"
    exit 1
fi

# ---- 2. 拉取远端 ----
OLD_HEAD=$(git rev-parse HEAD)
git fetch origin "$BRANCH" --quiet
git fetch origin deploy --quiet 2>/dev/null || true
NEW_HEAD=$(git rev-parse "origin/$BRANCH")

if [ "$OLD_HEAD" = "$NEW_HEAD" ]; then
    log "up-to-date: $NEW_HEAD"
    exit 0
fi

# CI 门禁：main 有新 commit 时，必须存在比该 commit 更新的 deploy 信号
# （GitHub Actions 在 CI 全绿后推送）。无信号 = CI 未通过或未跑，不部署。
SIGNAL_COMMIT=$(git log origin/deploy -1 --format=%H 2>/dev/null || echo "")
if [ -z "$SIGNAL_COMMIT" ]; then
    log "HOLD: main updated but no deploy signal (CI not green yet) — skipping"
    exit 0
fi
# 信号分支最新 SIGNAL 文件里记录的 commit 是否覆盖当前 main 头
SIGNAL_TARGET=$(git show origin/deploy:SIGNAL 2>/dev/null | grep '^commit:' | awk '{print $2}')
if [ "$SIGNAL_TARGET" != "$NEW_HEAD" ]; then
    log "HOLD: deploy signal targets ${SIGNAL_TARGET:-?} != main HEAD $NEW_HEAD (CI pending) — skipping"
    exit 0
fi
log "deploying: $OLD_HEAD → $NEW_HEAD (CI green ✓)"

# 记住部署前状态用于回滚
echo "$OLD_HEAD" > /var/lib/mychat-deploy/rollback_commit

git reset --hard "origin/$BRANCH" --quiet
log "checked out origin/$BRANCH"

# ---- 3. 后端依赖：requirements.txt 变了才重装（hash 比对，幂等） ----
REQ_HASH_FILE=/var/lib/mychat-deploy/requirements.sha256
NEW_REQ_HASH=$(sha256sum backend/requirements.txt | awk '{print $1}')
OLD_REQ_HASH=$(cat "$REQ_HASH_FILE" 2>/dev/null || echo "none")
if [ "$NEW_REQ_HASH" != "$OLD_REQ_HASH" ]; then
    log "requirements.txt changed — reinstalling venv deps"
    "$VENV/bin/pip" install -r backend/requirements.txt -q
    echo "$NEW_REQ_HASH" > "$REQ_HASH_FILE"
else
    log "requirements unchanged — skip pip install"
fi

# ---- 4. 前端构建（总是构建——NEXT_PUBLIC_* 在构建时内联，必须重建） ----
log "building frontend..."
cd frontend
if [ ! -d node_modules ]; then
    npm ci --no-audit --no-fund --silent
fi
NEXT_PUBLIC_API_BASE_URL=https://mychat.qiangi.top \
NEXT_TELEMETRY_DISABLED=1 \
    npm run build --silent
cd "$REPO"

# ---- 5. 数据库迁移（迁移失败必须中止部署：带缺口 schema 上线必然不可用） ----
log "applying alembic migrations (if any)..."
cd backend
set -a; . "$ENV_FILE"; set +a
if ! MIG_OUT=$("$VENV/bin/alembic" upgrade head 2>&1); then
    log "FATAL: alembic upgrade failed — services NOT restarted, aborting deploy:"
    echo "$MIG_OUT" | tail -10 | while read -r line; do log "alembic: $line"; done
    cd "$REPO"
    git reset --hard "$OLD_HEAD" --quiet
    exit 1
fi
log "alembic: $(echo "$MIG_OUT" | tail -1)"
cd "$REPO"

# ---- 6. 重启服务 ----
log "restarting services..."
systemctl restart mychat-backend mychat-frontend
# Best-effort: restart the durable worker/recovery units too when present.
# A worker skipped here keeps executing runs with OLD code (durable turns run
# in the worker process, not in the backend) — restarting the backend alone
# is not enough on stacks with BACKGROUND_WORKER=durable.
systemctl restart mychat-worker 2>/dev/null || log "no mychat-worker unit (skipped)"
systemctl restart mychat-recovery 2>/dev/null || log "no mychat-recovery unit (skipped)"

# ---- 7. 健康验证（失败回滚） ----
healthy=0
for i in $(seq 1 $HEALTH_RETRIES); do
    sleep 2
    if curl -sf --max-time 5 "$BACKEND_READY_URL" >/dev/null 2>&1; then
        healthy=1
        break
    fi
done

if [ "$healthy" != "1" ]; then
    # 回滚只回退代码，数据库保持在新 revision（expand-contract 约定：迁移只
    # 做 additive 变更）。/ready 的迁移检查允许 DB 领先于代码 head，因此回滚
    # 后健康检查可以通过；禁止在迁移里写破坏性 contract 变更（删列/删表必须
    # 推迟到下一个版本）。
    log "UNHEALTHY after deploy — rolling back to $OLD_HEAD"
    ROLLBACK_TO=$(cat /var/lib/mychat-deploy/rollback_commit 2>/dev/null || echo "$OLD_HEAD")
    git reset --hard "$ROLLBACK_TO" --quiet
    cd frontend && NEXT_PUBLIC_API_BASE_URL=https://mychat.qiangi.top NEXT_TELEMETRY_DISABLED=1 npm run build --silent && cd "$REPO"
    systemctl restart mychat-backend mychat-frontend
    sleep 6
    if curl -sf --max-time 5 "$BACKEND_READY_URL" >/dev/null 2>&1; then
        log "ROLLBACK OK — serving $ROLLBACK_TO"
    else
        log "ROLLBACK ALSO UNHEALTHY — manual intervention required"
        exit 2
    fi
    exit 1
fi

echo "$NEW_HEAD" > "$STATE_FILE"
FRONT_OK=$(curl -sf -o /dev/null -w "%{http_code}" --max-time 5 http://127.0.0.1:5003/ || echo 000)
log "DEPLOYED $NEW_HEAD (frontend http=$FRONT_OK)"
