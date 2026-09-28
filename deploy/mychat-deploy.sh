#!/usr/bin/env bash
# Deploy only CI-published, immutable GHCR images. The server does not build code.
set -Eeuo pipefail

REPO=${MYCHAT_REPO:-/root/MyGPT}
ENV_FILE=${MYCHAT_ENV_FILE:-/root/MyGPT/.env}
STATE_DIR=/var/lib/mychat-deploy
STATE_FILE=$STATE_DIR/last_deployed_commit
RELEASES_DIR=/opt/mychat-deploy/releases
BACKEND_READY_URL=http://127.0.0.1:8003/ready
FRONTEND_READY_URL=http://127.0.0.1:5003/
LOG_TAG=mychat-deploy
HEALTH_RETRIES=90

log() { logger -t "$LOG_TAG" -- "$*" 2>/dev/null || echo "[$(date '+%F %T')] $*"; }
fatal() { log "ERROR: $*"; exit 1; }

mkdir -p "$STATE_DIR" "$RELEASES_DIR"
install -d -m 1777 /opt/mychat-data/sandbox
[[ -d "$REPO/.git" ]] || fatal "repository not found: $REPO"
[[ -r "$ENV_FILE" ]] || fatal "production env file is missing or unreadable: $ENV_FILE"
command -v docker >/dev/null || fatal "docker is not installed"
docker compose version >/dev/null 2>&1 || fatal "Docker Compose plugin is required (docker compose)"
command -v curl >/dev/null || fatal "curl is not installed"
command -v flock >/dev/null || fatal "flock is not installed"
exec 9>/run/lock/mychat-deploy.lock
flock -n 9 || fatal "another deployment is already running"

# Refuse to deploy on top of operator edits, and never reset or clean the tree.
if ! git -C "$REPO" diff --quiet || ! git -C "$REPO" diff --cached --quiet; then
    fatal "repository has tracked local changes; refusing deployment"
fi

# Production settings are trusted operator input. Loading them here allows
# Compose to interpolate paths, while values are never printed to the journal.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

[[ -n "${REDEEM_CODE_PEPPER:-}" ]] || fatal "REDEEM_CODE_PEPPER is missing in the production env file"
[[ ${#REDEEM_CODE_PEPPER} -ge 32 ]] || fatal "REDEEM_CODE_PEPPER must be at least 32 characters"
if [[ "${REDEEM_CODE_PEPPER,,}" =~ (changeme|please-change|example|placeholder|your[-_]) ]]; then
    fatal "REDEEM_CODE_PEPPER still looks like a placeholder"
fi
[[ -n "${STORAGE_DIR:-}" && "$STORAGE_DIR" == /* ]] || fatal "STORAGE_DIR must be an absolute path"
[[ -d "$STORAGE_DIR" && -w "$STORAGE_DIR" ]] || fatal "STORAGE_DIR is missing or not writable"
[[ "${SANDBOX_MODE:-}" != docker || -n "${DOCKER_HOST:-}" ]] || \
    fatal "SANDBOX_MODE=docker requires DOCKER_HOST for the separate sandbox daemon"

git -C "$REPO" fetch --quiet origin main deploy || fatal "cannot fetch origin main/deploy"
SIGNAL=$(git -C "$REPO" show origin/deploy:SIGNAL 2>/dev/null || true)
TARGET_SHA=$(printf '%s\n' "$SIGNAL" | awk '$1 == "commit:" {print $2; exit}')
[[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || fatal "deploy signal is missing a valid commit SHA"
git -C "$REPO" cat-file -e "$TARGET_SHA^{commit}" 2>/dev/null || \
    fatal "deploy signal commit is not available locally: $TARGET_SHA"
git -C "$REPO" merge-base --is-ancestor "$TARGET_SHA" origin/main || \
    fatal "deploy signal is not an ancestor of origin/main"

PREVIOUS_SHA=$(cat "$STATE_FILE" 2>/dev/null || true)
if [[ "$PREVIOUS_SHA" == "$TARGET_SHA" ]]; then
    log "up-to-date: $TARGET_SHA"
    exit 0
fi
if [[ "$PREVIOUS_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    git -C "$REPO" merge-base --is-ancestor "$PREVIOUS_SHA" "$TARGET_SHA" || \
        fatal "refusing stale or non-fast-forward deployment: $PREVIOUS_SHA -> $TARGET_SHA"
fi

COMPOSE_FILE="$RELEASES_DIR/$TARGET_SHA/docker-compose.server.yml"
mkdir -p "$(dirname "$COMPOSE_FILE")"
git -C "$REPO" show "$TARGET_SHA:deploy/docker-compose.server.yml" > "$COMPOSE_FILE" || \
    fatal "commit $TARGET_SHA does not contain deploy/docker-compose.server.yml"
chmod 0644 "$COMPOSE_FILE"
export ENV_FILE IMAGE_TAG="sha-$TARGET_SHA"

compose() {
    docker compose --project-name mychat --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

log "pulling immutable images for $TARGET_SHA"
compose pull backend frontend worker recovery || fatal "image pull failed; running services were not stopped"

# Apply schema changes while the existing app is still available. Migrations
# must follow the expand/contract rule so the currently serving version remains
# compatible with the new schema until the container health gate passes.
compose run --rm migrate || fatal "database migration failed; existing services were not stopped"

stop_native() {
    local unit
    for unit in mychat-backend.service mychat-frontend.service mychat-worker.service mychat-recovery.service; do
        if systemctl cat "$unit" >/dev/null 2>&1; then
            systemctl stop "$unit" || return 1
            systemctl disable "$unit" || return 1
        fi
    done
}

start_native() {
    local unit
    for unit in mychat-backend.service mychat-frontend.service mychat-worker.service mychat-recovery.service; do
        if systemctl cat "$unit" >/dev/null 2>&1; then
            systemctl enable --now "$unit" || return 1
        fi
    done
}

wait_healthy() {
    local i service container_id health
    for ((i=1; i<=HEALTH_RETRIES; i++)); do
        if curl -fsS --max-time 5 "$BACKEND_READY_URL" >/dev/null 2>&1 && \
           curl -fsS --max-time 5 "$FRONTEND_READY_URL" >/dev/null 2>&1; then
            for service in backend frontend worker recovery; do
                container_id=$(compose ps -q "$service")
                health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || true)
                [[ "$health" == healthy ]] || break
            done
            [[ "$service" == recovery && "$health" == healthy ]] && return 0
        fi
        sleep 2
    done
    return 1
}

rollback() {
    log "deployment health check failed; rolling back"
    if [[ "$PREVIOUS_SHA" =~ ^[0-9a-f]{40}$ ]]; then
        export IMAGE_TAG="sha-$PREVIOUS_SHA"
        if compose pull backend frontend worker recovery && \
           compose up -d backend frontend worker recovery && wait_healthy; then
            log "rollback successful: $PREVIOUS_SHA"
            return 0
        fi
        log "container rollback failed; trying the previous systemd services"
    fi
    compose down || true
    if start_native; then
        log "native systemd services restarted"
        return 0
    fi
    log "rollback failed; manual intervention required"
    return 1
}

if ! stop_native; then
    start_native || log "could not restore every native service after stopping them"
    fatal "could not stop and disable all native services; refusing container cutover"
fi
if ! compose up -d backend frontend worker recovery || ! wait_healthy; then
    rollback || exit 2
    exit 1
fi

printf '%s\n' "$TARGET_SHA" > "$STATE_FILE.tmp"
chmod 0644 "$STATE_FILE.tmp"
mv "$STATE_FILE.tmp" "$STATE_FILE"
log "deployment successful: $TARGET_SHA"
