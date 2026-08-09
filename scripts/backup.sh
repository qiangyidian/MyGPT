#!/usr/bin/env bash
# MyGPT backup — Postgres (pg_dump) + Qdrant (snapshot API) into a timestamped dir.
# Run via cron (e.g. daily). Restore with restore.sh.
#
# Configure via env (defaults match docker-compose):
#   PG_HOST, PG_PORT, PG_USER, PG_DB, PGPASSWORD
#   QDRANT_URL (default http://localhost:6333)
#   BACKUP_DIR (default ./backups)
#   RETAIN_DAYS (default 14)  — prune backups older than this
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./backups}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
RETAIN_DAYS="${RETAIN_DAYS:-14}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${BACKUP_DIR}/${TS}"
mkdir -p "${DEST}"

echo "[backup] → ${DEST}"

# 1. Postgres logical dump (custom format, parallel-restore-friendly).
PGPASSWORD="${PGPASSWORD:-postgres}" pg_dump \
  -h "${PG_HOST:-localhost}" -p "${PG_PORT:-5432}" \
  -U "${PG_USER:-postgres}" -d "${PG_DB:-ai_chat}" \
  -F c -f "${DEST}/postgres.dump"
echo "[backup] postgres OK ($(du -h "${DEST}/postgres.dump" | cut -f1))"

# 2. Qdrant: create a snapshot of every collection, then download the tarball.
COLLECTIONS="$(curl -fsS "${QDRANT_URL}/collections" | python -c 'import sys,json; print("\n".join(json.load(sys.stdin)["result"]["collections"].keys()))' || true)"
for c in ${COLLECTIONS}; do
  curl -fsS -X PUT "${QDRANT_URL}/collections/${c}/snapshots" >/dev/null
  SNAP="$(curl -fsS "${QDRANT_URL}/collections/${c}/snapshots" | python -c 'import sys,json; print(json.load(sys.stdin)["result"][-1]["name"])')"
  curl -fsS "${QDRANT_URL}/collections/${c}/snapshots/${SNAP}" -o "${DEST}/qdrant-${c}.snapshot"
done
echo "[backup] qdrant OK (${COLLECTIONS:-no collections})"

# 3. Object-storage uploads (local backend only; S3/MinIO version themselves).
if [ -d "./backend/data/uploads" ]; then
  tar -cf "${DEST}/uploads.tar" -C ./backend/data uploads 2>/dev/null || true
fi

# 4. Prune old backups.
find "${BACKUP_DIR}" -maxdepth 1 -type d -mtime +${RETAIN_DAYS} -exec rm -rf {} \; 2>/dev/null || true
echo "[backup] done (retaining ${RETAIN_DAYS}d)"
