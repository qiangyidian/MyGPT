#!/usr/bin/env bash
# Task 13 — migration-head verification.
#
# Runs Alembic migrations against ISOLATED throwaway Postgres databases and
# asserts both reach the repo head (0010_artifacts). Nothing touches the dev or
# production database.
#
# Two paths are exercised:
#   1. EMPTY      — a fresh DB upgraded from zero -> head.
#   2. INCREMENTAL— a fresh DB upgraded to the PRIOR revision (0009_connectors)
#                    then to head, proving an existing deployment at the
#                    previous head upgrades cleanly (the real deploy path).
#
# Requires: docker (for the isolated postgres) + the backend venv (psycopg2).
# Usage:
#   ./scripts/verify_migrations.sh
#   PG_PORT=55432 REPO_HEAD=0010_artifacts ./scripts/verify_migrations.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

# Incremental path exercises "prior revision -> head"; derive the prior
# revision from the head migration's down_revision instead of hardcoding it.
resolve_prior_rev() {
  local head_file
  head_file="$(grep -rlE "^revision(:[^=]*)?= *['\"]${REPO_HEAD}['\"]" backend/migrations/versions 2>/dev/null | head -n1)"
  [ -n "$head_file" ] || return 0
  sed -nE "s/^down_revision(:[^=]*)?= *['\"]([^'\"]+)['\"].*/\2/p" "$head_file" | head -n1
}
PRIOR_REV="${PRIOR_REV:-$(resolve_prior_rev)}"
PG_PORT="${PG_PORT:-55432}"
PG_IMAGE="${PG_IMAGE:-postgres:16-alpine}"
CTR="mygpt-verify-pg-$$"

# Locate alembic via the backend venv's `python -m alembic` (cross-platform:
# Windows venv is .venv/Scripts/python.exe, Linux is .venv/bin/python). Falls
# back to `alembic` on PATH. Absolute paths so the `cd backend` in invocations
# (needed to find alembic.ini) doesn't break a relative python path.
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
  echo "[verify] tearing down $CTR"
  docker rm -f "$CTR" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[verify] starting isolated postgres ($PG_IMAGE) on 127.0.0.1:$PG_PORT"
docker run -d --name "$CTR" \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres \
  -p 127.0.0.1:${PG_PORT}:5432 "$PG_IMAGE" >/dev/null

# Wait for the postgres container to accept connections.
echo -n "[verify] waiting for postgres"
for _ in $(seq 1 30); do
  if docker exec "$CTR" pg_isready -U postgres >/dev/null 2>&1; then
    echo " up"
    break
  fi
  echo -n "."
  sleep 1
done
docker exec "$CTR" pg_isready -U postgres >/dev/null 2>&1 || { echo "postgres never came up" >&2; exit 1; }

# Assert the repo has exactly one alembic head (no branching) == REPO_HEAD.
echo "[verify] alembic heads (expect single head $REPO_HEAD)"
HEADS="$(cd backend && "${ALEMBIC_CMD[@]}" heads 2>/dev/null | awk '{print $1}' | sort -u || true)"
if ! grep -qx "$REPO_HEAD" <<<"$HEADS"; then
  echo "[verify] FAIL: alembic heads = $(tr '\n' ' ' <<<"$HEADS"); expected $REPO_HEAD" >&2
  exit 1
fi

run_alembic() {  # $1 = db name, $2 = revision (or head)
  ( cd backend && DATABASE_URL="postgresql+asyncpg://postgres:postgres@127.0.0.1:${PG_PORT}/$1" "${ALEMBIC_CMD[@]}" upgrade "$2" )
}
current_rev() {  # $1 = db name -> echoes the current revision
  ( cd backend && DATABASE_URL="postgresql+asyncpg://postgres:postgres@127.0.0.1:${PG_PORT}/$1" "${ALEMBIC_CMD[@]}" current 2>/dev/null | awk '{print $1}' | head -1 )
}

assert_head() {  # $1 = label, $2 = db name
  local label="$1" db="$2" cur
  cur="$(current_rev "$db")"
  echo "[verify] $label: alembic current = ${cur:-<none>}"
  if [ "$cur" != "$REPO_HEAD" ]; then
    echo "[verify] FAIL: $label did not reach head $REPO_HEAD (got ${cur:-<none>})" >&2
    exit 1
  fi
}

# --- Path 1: empty DB -> head ------------------------------------------------
docker exec "$CTR" createdb -U postgres verify_empty
echo "[verify] path 1: empty DB -> upgrade head"
run_alembic verify_empty head
assert_head "empty" verify_empty

# --- Path 2: incremental (prior revision -> head) ----------------------------
if [ -z "$PRIOR_REV" ]; then
  echo "[verify] SKIP path 2: head has no down_revision (single-migration repo)"
  echo "[verify] PASS: empty path at head $REPO_HEAD"
  exit 0
fi
docker exec "$CTR" createdb -U postgres verify_inc
echo "[verify] path 2: incremental DB -> upgrade $PRIOR_REV then head"
run_alembic verify_inc "$PRIOR_REV"
mid="$(current_rev verify_inc)"
echo "[verify] incremental intermediate = ${mid:-<none>} (expect $PRIOR_REV)"
[ "$mid" = "$PRIOR_REV" ] || { echo "[verify] FAIL: incremental did not stop at $PRIOR_REV" >&2; exit 1; }
run_alembic verify_inc head
assert_head "incremental" verify_inc

echo "[verify] PASS: empty + incremental paths both at head $REPO_HEAD"
