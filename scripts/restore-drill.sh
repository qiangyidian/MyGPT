#!/usr/bin/env bash
# Task 13 — backup restore drill.
#
# Restores a backup produced by scripts/backup.sh into ISOLATED throwaway
# Postgres + Qdrant containers (never the dev/prod data stores) and verifies:
#   1. Postgres: the dump restores cleanly AND alembic current == repo head
#      (0010_artifacts) — i.e. the migration revision survives backup/restore.
#   2. Qdrant:   every collection snapshot uploads and the collection is listed.
#   3. Object    storage: uploads.tar extracts without error and the per-file
#      sha256 of the extracted tree matches a re-extraction (tar round-trip
#      integrity). If the backup dir carries a MANIFEST.sha256, it is verified
#      against it instead.
#
# Requires: docker, the backend venv (psycopg2 + alembic), curl, sha256sum.
# Usage:
#   ./scripts/restore-drill.sh <backup-dir>
#   ./scripts/restore-drill.sh ./backups/20260101T000000Z
#   PG_PORT=55433 QDRANT_PORT=6334 ./scripts/restore-drill.sh <backup-dir>
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

SRC="${1:-${BACKUP_DIR:-}}"
if [ -z "$SRC" ] || [ ! -d "$SRC" ]; then
  echo "usage: $0 <backup-dir>  (e.g. ./backups/20260101T000000Z)" >&2
  exit 1
fi

PG_PORT="${PG_PORT:-55433}"
QDRANT_PORT="${QDRANT_PORT:-6334}"
PG_CTR="mygpt-drill-pg-$$"
QDRANT_CTR="mygpt-drill-qdrant-$$"

# Locate alembic via the backend venv's `python -m alembic` (cross-platform).
# Absolute paths so the `cd backend` in invocations doesn't break the path.
ALEMBIC_CMD=()
if   [ -x "$REPO_DIR/backend/.venv/Scripts/python.exe" ]; then ALEMBIC_CMD=("$REPO_DIR/backend/.venv/Scripts/python.exe" -m alembic)
elif [ -x "$REPO_DIR/backend/.venv/bin/python" ];        then ALEMBIC_CMD=("$REPO_DIR/backend/.venv/bin/python" -m alembic)
else                                                          ALEMBIC_CMD=(alembic)
fi

# Resolve the repo's alembic head dynamically (a hardcoded head drifted from
# reality once and broke /ready for every migration-carrying deploy).
resolve_head() {
  ( cd backend && "${ALEMBIC_CMD[@]}" heads 2>/dev/null | awk '{print $1}' | head -n1 )
}
REPO_HEAD="${REPO_HEAD:-$(resolve_head)}"
if [ -z "$REPO_HEAD" ]; then
  echo "[verify] FAIL: cannot resolve alembic head from backend/migrations" >&2
  exit 1
fi

cleanup() {
  echo "[drill] tearing down $PG_CTR + $QDRANT_CTR"
  docker rm -f "$PG_CTR" "$QDRANT_CTR" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[drill] source: $SRC"

# --- isolated targets --------------------------------------------------------
echo "[drill] starting isolated postgres on 127.0.0.1:$PG_PORT"
docker run -d --name "$PG_CTR" \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=ai_chat \
  -p 127.0.0.1:${PG_PORT}:5432 postgres:16-alpine >/dev/null
echo "[drill] starting isolated qdrant on 127.0.0.1:$QDRANT_PORT"
docker run -d --name "$QDRANT_CTR" \
  -p 127.0.0.1:${QDRANT_PORT}:6333 qdrant/qdrant:v1.12.4 >/dev/null

echo -n "[drill] waiting for postgres"
for _ in $(seq 1 30); do
  docker exec "$PG_CTR" pg_isready -U postgres >/dev/null 2>&1 && { echo " up"; break; }
  echo -n "."; sleep 1
done
docker exec "$PG_CTR" pg_isready -U postgres >/dev/null 2>&1 || { echo "postgres never came up" >&2; exit 1; }
echo -n "[drill] waiting for qdrant"
for _ in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:${QDRANT_PORT}/readyz" >/dev/null 2>&1 && { echo " up"; break; }
  curl -fsS "http://127.0.0.1:${QDRANT_PORT}/" >/dev/null 2>&1 && { echo " up"; break; }
  echo -n "."; sleep 1
done
curl -fsS "http://127.0.0.1:${QDRANT_PORT}/collections" >/dev/null 2>&1 || { echo "qdrant never came up" >&2; exit 1; }

fail=0

# --- 1. Postgres restore + migration revision check --------------------------
if [ -f "$SRC/postgres.dump" ]; then
  echo "[drill] restoring postgres.dump -> isolated"
  PGPASSWORD=postgres pg_restore \
    -h 127.0.0.1 -p "$PG_PORT" -U postgres -d ai_chat \
    --no-owner --no-privileges --clean --if-exists -j 4 "$SRC/postgres.dump" || {
      # --clean on an empty db can emit harmless "relation does not exist" notices;
      # a real failure exits non-zero on the dump itself. Retry without --clean.
      PGPASSWORD=postgres pg_restore \
        -h 127.0.0.1 -p "$PG_PORT" -U postgres -d ai_chat \
        --no-owner --no-privileges -j 4 "$SRC/postgres.dump"
    }
  CUR="$(cd backend && DATABASE_URL="postgresql+asyncpg://postgres:postgres@127.0.0.1:${PG_PORT}/ai_chat" "${ALEMBIC_CMD[@]}" current 2>/dev/null | awk '{print $1}' | head -1)"
  echo "[drill] restored alembic current = ${CUR:-<none>} (expect $REPO_HEAD)"
  [ "$CUR" = "$REPO_HEAD" ] || { echo "[drill] FAIL: migration revision != head" >&2; fail=1; }
else
  echo "[drill] SKIP: no postgres.dump in $SRC" >&2
  fail=1
fi

# --- 2. Qdrant snapshot restore ---------------------------------------------
if ls "$SRC"/qdrant-*.snapshot >/dev/null 2>&1; then
  n=0
  for snap in "$SRC"/qdrant-*.snapshot; do
    c="$(basename "$snap" .snapshot | sed 's/^qdrant-//')"
    curl -fsS -X PUT "http://127.0.0.1:${QDRANT_PORT}/collections/${c}/snapshots/upload" \
      -H "Content-Type: multipart/form-data" -F "file=@${snap}" >/dev/null
    n=$((n+1))
  done
  RESTORED="$(curl -fsS "http://127.0.0.1:${QDRANT_PORT}/collections" | python -c 'import sys,json; print(len(json.load(sys.stdin)["result"]["collections"]))' 2>/dev/null || echo 0)"
  echo "[drill] restored $n qdrant snapshot(s); collections present = $RESTORED"
  [ "$RESTORED" -ge "$n" ] || { echo "[drill] FAIL: qdrant collection count < snapshots" >&2; fail=1; }
else
  echo "[drill] SKIP: no qdrant-*.snapshot in $SRC (ok if no collections were backed up)"
fi

# --- 3. Object storage (uploads.tar) round-trip + checksums -----------------
if [ -f "$SRC/uploads.tar" ]; then
  TMP_A="$(mktemp -d)"; TMP_B="$(mktemp -d)"
  tar -xf "$SRC/uploads.tar" -C "$TMP_A"
  # If the backup carries a MANIFEST.sha256, verify against it; else verify the
  # tar round-trips by re-extracting and comparing the two trees' checksums.
  if [ -f "$SRC/MANIFEST.sha256" ]; then
    ( cd "$TMP_A" && sha256sum -c "$SRC/MANIFEST.sha256" >/dev/null 2>&1 ) \
      && echo "[drill] uploads MANIFEST.sha256 verified" \
      || { echo "[drill] FAIL: uploads MANIFEST.sha256 mismatch" >&2; fail=1; }
  else
    tar -xf "$SRC/uploads.tar" -C "$TMP_B"
    A_SUMS="$(cd "$TMP_A" && find . -type f -exec sha256sum {} \; | sort)"
    B_SUMS="$(cd "$TMP_B" && find . -type f -exec sha256sum {} \; | sort)"
    if [ "$A_SUMS" = "$B_SUMS" ]; then
      echo "[drill] uploads tar round-trip OK ($(printf '%s\n' "$A_SUMS" | grep -c .) files)"
    else
      echo "[drill] FAIL: uploads tar round-trip checksum mismatch" >&2; fail=1
    fi
  fi
  rm -rf "$TMP_A" "$TMP_B"
else
  echo "[drill] SKIP: no uploads.tar in $SRC"
fi

if [ "$fail" -ne 0 ]; then
  echo "[drill] RESULT: FAIL (see above)"
  exit 1
fi
echo "[drill] RESULT: PASS — postgres@head, qdrant restored, uploads verified"
echo "[drill] note: for an end-to-end artifact round-trip, insert a probe"
echo "       artifact via the API before backup, then query it after restore."
