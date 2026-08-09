#!/usr/bin/env bash
# MyGPT restore from a backup dir produced by backup.sh.
# Usage: BACKUP_DIR=./backups/<TS> ./scripts/restore.sh
#   PG_HOST, PG_PORT, PG_USER, PG_DB, PGPASSWORD, QDRANT_URL as in backup.sh
set -euo pipefail

SRC="${1:-${BACKUP_DIR:-}}"
if [ -z "${SRC}" ] || [ ! -d "${SRC}" ]; then
  echo "usage: $0 <backup-dir>  (e.g. ./backups/20260101T000000Z)" >&2
  exit 1
fi
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
echo "[restore] ← ${SRC}"

# 1. Postgres: wipe + restore from the custom-format dump.
PGPASSWORD="${PGPASSWORD:-postgres}" pg_restore \
  -h "${PG_HOST:-localhost}" -p "${PG_PORT:-5432}" \
  -U "${PG_USER:-postgres}" -d "${PG_DB:-ai_chat}" \
  --clean --if-exists --no-owner -j 4 "${SRC}/postgres.dump"
echo "[restore] postgres OK"

# 2. Qdrant: restore each collection snapshot.
for snap in "${SRC}"/qdrant-*.snapshot; do
  [ -e "$snap" ] || continue
  c="$(basename "$snap" .snapshot | sed 's/^qdrant-//')"
  curl -fsS -X PUT "${QDRANT_URL}/collections/${c}/snapshots/upload" \
    -H "Content-Type: multipart/form-data" -F "file=@${snap}" >/dev/null
done
echo "[restore] qdrant OK"

# 3. Uploads (local backend).
if [ -f "${SRC}/uploads.tar" ]; then
  mkdir -p ./backend/data && tar -xf "${SRC}/uploads.tar" -C ./backend/data
  echo "[restore] uploads OK"
fi
echo "[restore] done"
