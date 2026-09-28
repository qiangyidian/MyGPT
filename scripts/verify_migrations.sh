#!/usr/bin/env bash
# Task 13 — migration-head verification.
#
# Runs Alembic migrations against ISOLATED throwaway Postgres databases and
# asserts both reach the repo head (resolved dynamically from `alembic heads`,
# never hardcoded — a pinned head here stops testing the real head). Nothing
# touches the dev or production database.
#
# Two paths are exercised:
#   1. EMPTY      — a fresh DB upgraded from zero -> head.
#   2. INCREMENTAL— a fresh DB upgraded to the head's own down_revision
#                    then to head, proving an existing deployment at the
#                    previous head upgrades cleanly (the real deploy path).
#
# Requires: Docker for local isolated runs; CI may set PG_EXTERNAL=1 to reuse its
# Postgres service. Both modes need the backend Python environment with asyncpg.
# Usage:
#   ./scripts/verify_migrations.sh
#   PG_PORT=55432 REPO_HEAD=0010_artifacts ./scripts/verify_migrations.sh
#   PG_EXTERNAL=1 PG_PORT=5432 PG_ADMIN_URL=... (GitHub Actions only)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

# Incremental path exercises "prior revision -> head"; derive the prior
# revision from the head migration's down_revision instead of hardcoding it.
#
# NOTE: this reads $REPO_HEAD, so it MUST be called after REPO_HEAD is resolved
# (further down). Calling it earlier made the reference an `unbound variable`
# under `set -u`; the function still exited 0 with empty output, so the
# incremental path — the one a real deploy actually takes — was silently
# SKIPPED and the script still printed PASS.
resolve_prior_rev() {
  local head_file
  head_file="$(grep -rlE "^revision(:[^=]*)?= *['\"]${REPO_HEAD}['\"]" backend/migrations/versions 2>/dev/null | head -n1)"
  [ -n "$head_file" ] || return 0
  sed -nE "s/^down_revision(:[^=]*)?= *['\"]([^'\"]+)['\"].*/\2/p" "$head_file" | head -n1
}
PG_PORT="${PG_PORT:-55432}"
PG_IMAGE="${PG_IMAGE:-postgres:16-alpine}"
CTR="mygpt-verify-pg-$$"
PG_EXTERNAL="${PG_EXTERNAL:-0}"
if [ "$PG_EXTERNAL" = "1" ] && [ "${GITHUB_ACTIONS:-false}" != "true" ]; then
  echo "[verify] PG_EXTERNAL=1 is reserved for the isolated GitHub Actions service" >&2
  exit 1
fi
PG_ADMIN_URL="${PG_ADMIN_URL:-postgresql://postgres:postgres@127.0.0.1:${PG_PORT}/postgres}"
if [ "$PG_EXTERNAL" = "1" ]; then
  DB_SUFFIX="${GITHUB_RUN_ID:-$$}_${GITHUB_RUN_ATTEMPT:-1}"
  VERIFY_EMPTY_DB="mygpt_verify_empty_${DB_SUFFIX}"
  VERIFY_INC_DB="mygpt_verify_inc_${DB_SUFFIX}"
else
  VERIFY_EMPTY_DB="verify_empty"
  VERIFY_INC_DB="verify_inc"
fi

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

# Resolved HERE (not at the top) because resolve_prior_rev reads REPO_HEAD.
PRIOR_REV="${PRIOR_REV:-$(resolve_prior_rev)}"

cleanup() {
  if [ "$PG_EXTERNAL" = "1" ]; then
    echo "[verify] dropping temporary databases from external Postgres"
    PG_ADMIN_URL="$PG_ADMIN_URL" VERIFY_EMPTY_DB="$VERIFY_EMPTY_DB" VERIFY_INC_DB="$VERIFY_INC_DB" python - <<'PY' || true
import asyncio
import os
import re
import asyncpg

async def main():
    conn = await asyncpg.connect(os.environ["PG_ADMIN_URL"])
    for name in (os.environ["VERIFY_INC_DB"], os.environ["VERIFY_EMPTY_DB"]):
        if not re.fullmatch(r"[a-zA-Z0-9_]+", name):
            raise ValueError("unsafe temporary database name")
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            name,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    await conn.close()

asyncio.run(main())
PY
  else
    echo "[verify] tearing down $CTR"
    docker rm -f "$CTR" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [ "$PG_EXTERNAL" = "1" ]; then
  echo "[verify] using the CI Postgres service on 127.0.0.1:$PG_PORT"
else
  echo "[verify] starting isolated postgres ($PG_IMAGE) on 127.0.0.1:$PG_PORT"
  docker run -d --name "$CTR" \
    -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres \
    -p 127.0.0.1:${PG_PORT}:5432 "$PG_IMAGE" >/dev/null
fi

# Wait for the postgres container to accept connections.
echo -n "[verify] waiting for postgres"
for _ in $(seq 1 30); do
  if [ "$PG_EXTERNAL" = "1" ]; then
    PG_ADMIN_URL="$PG_ADMIN_URL" python - <<'PY' >/dev/null 2>&1 && ready=1 || ready=0
import asyncio, os, asyncpg
async def main():
    conn = await asyncpg.connect(os.environ["PG_ADMIN_URL"])
    await conn.close()
asyncio.run(main())
PY
  else
    docker exec "$CTR" pg_isready -U postgres >/dev/null 2>&1 && ready=1 || ready=0
  fi
  if [ "${ready:-0}" = "1" ]; then
    echo " up"
    break
  fi
  echo -n "."
  sleep 1
done
if [ "$PG_EXTERNAL" = "1" ]; then
  [ "${ready:-0}" = "1" ] || { echo "postgres service never became available" >&2; exit 1; }
else
  docker exec "$CTR" pg_isready -U postgres >/dev/null 2>&1 || { echo "postgres never came up" >&2; exit 1; }
fi

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
if [ "$PG_EXTERNAL" = "1" ]; then
  PG_ADMIN_URL="$PG_ADMIN_URL" python - "$VERIFY_EMPTY_DB" <<'PY'
import asyncio, os, sys, re, asyncpg
async def main():
    if not re.fullmatch(r"[a-zA-Z0-9_]+", sys.argv[1]):
        raise ValueError("unsafe temporary database name")
    conn = await asyncpg.connect(os.environ["PG_ADMIN_URL"])
    await conn.execute(f'CREATE DATABASE "{sys.argv[1]}"')
    await conn.close()
asyncio.run(main())
PY
else
  docker exec "$CTR" createdb -U postgres "$VERIFY_EMPTY_DB"
fi
echo "[verify] path 1: empty DB -> upgrade head"
run_alembic "$VERIFY_EMPTY_DB" head
assert_head "empty" "$VERIFY_EMPTY_DB"

# --- Path 2: incremental (prior revision -> head) ----------------------------
if [ -z "$PRIOR_REV" ]; then
  echo "[verify] SKIP path 2: head has no down_revision (single-migration repo)"
  echo "[verify] PASS: empty path at head $REPO_HEAD"
  exit 0
fi
if [ "$PG_EXTERNAL" = "1" ]; then
  PG_ADMIN_URL="$PG_ADMIN_URL" python - "$VERIFY_INC_DB" <<'PY'
import asyncio, os, sys, re, asyncpg
async def main():
    if not re.fullmatch(r"[a-zA-Z0-9_]+", sys.argv[1]):
        raise ValueError("unsafe temporary database name")
    conn = await asyncpg.connect(os.environ["PG_ADMIN_URL"])
    await conn.execute(f'CREATE DATABASE "{sys.argv[1]}"')
    await conn.close()
asyncio.run(main())
PY
else
  docker exec "$CTR" createdb -U postgres "$VERIFY_INC_DB"
fi
echo "[verify] path 2: incremental DB -> upgrade $PRIOR_REV then head"
run_alembic "$VERIFY_INC_DB" "$PRIOR_REV"
mid="$(current_rev "$VERIFY_INC_DB")"
echo "[verify] incremental intermediate = ${mid:-<none>} (expect $PRIOR_REV)"
[ "$mid" = "$PRIOR_REV" ] || { echo "[verify] FAIL: incremental did not stop at $PRIOR_REV" >&2; exit 1; }
run_alembic "$VERIFY_INC_DB" head
assert_head "incremental" "$VERIFY_INC_DB"

echo "[verify] PASS: empty + incremental paths both at head $REPO_HEAD"
