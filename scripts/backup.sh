#!/usr/bin/env bash
# MyGPT backup — Postgres (pg_dump) + Qdrant (snapshot API) + uploads tar, into a
# timestamped dir, then duplicated OFFSITE (scripts/sync-offsite.sh).
#
# Run via the systemd timer (deploy/mychat-backup.{service,timer}) or cron.
# Restore with scripts/restore.sh; the drill is scripts/restore-drill.sh; the
# RPO/RTO contract and the on-call procedure live in docs/backup-restore.md.
#
# Credentials/config resolve in this order: explicit env > the repo .env
# (chmod 600 on the server) > the defaults below. Set:
#   DATABASE_URL                      parsed for host/port/user/password/db
#   PG_HOST, PG_PORT, PG_USER, PG_DB, PGPASSWORD   (each wins over DATABASE_URL)
#   QDRANT_URL (default http://localhost:6333), QDRANT_API_KEY
#   STORAGE_DIR                       where the backend writes uploads
#   BACKUP_DIR (default <repo>/backups)
#   RETAIN_DAYS (default 14)          — LOCAL prune
# Offsite (implemented in scripts/sync-offsite.sh — read its header first):
#   BACKUP_RCLONE_REMOTE, BACKUP_RCLONE_PREFIX, RETAIN_REMOTE_DAYS (default 30)
#   BACKUP_SKIP_OFFSITE=1             local half only; prints a loud SKIP line
# Alerting:
#   BACKUP_ALERT_URL                  a 企业微信/钉钉 robot webhook (same receiver
#                                     class as ops-critical in
#                                     deploy/monitoring/alertmanager.yml).
#                                     Unset is allowed, but then the journal and
#                                     `systemctl --failed` are the ONLY record —
#                                     see backup_failed below.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_ROOT_ENV="${BACKUP_DIR:-}"
BACKUP_ALERT_URL="${BACKUP_ALERT_URL:-}"

# ------------------------------------------------------------------ alerting --
# Every failure in this script funnels through backup_failed, from two places:
# the ERR trap (an unexpected non-zero command under `set -e`) and die() (an
# explicit integrity check). Either way the script then exits non-zero, so the
# unit is reported as failed AND the local prune below is never reached — a batch
# that never made it off the machine must not also delete the copies that did.
# With no webhook configured the function says so on stderr instead of pretending
# somebody was notified: Alertmanager/Prometheus do NOT scrape systemd oneshots,
# so journalctl + `systemctl --failed` really are the fallback channel.
job_state="running"
backup_failed() {
  local rc="${1:-1}" line="${2:-?}"
  # Single-shot: also blocks trap recursion (a failing command INSIDE the handler
  # would otherwise re-enter ERR) and suppresses a late page after the success line.
  if [ "${job_state}" != "running" ]; then
    return 0
  fi
  job_state="alerted"
  echo "[backup] FAILED (exit ${rc}, line ${line}) —— 本批次不可信，本地旧批次也未被清理" >&2
  if [ -n "${BACKUP_ALERT_URL}" ]; then
    curl -fsS -m 20 -X POST -H 'Content-Type: application/json' \
      -d "{\"text\":\"[MyGPT] 备份任务失败：$(hostname 2>/dev/null || echo unknown-host) ${DEST:-未建立备份目录} exit=${rc} line=${line} — journalctl -u mychat-backup.service\"}" \
      "${BACKUP_ALERT_URL}" >/dev/null 2>&1 \
      || echo "[backup] WARN: BACKUP_ALERT_URL 已配置但推送未成功 —— 回落到 journal，请用 journalctl -u mychat-backup.service 查" >&2
  else
    echo "[backup] WARN: 未配置 BACKUP_ALERT_URL —— 本次失败无人被通知，只能查 journalctl -u mychat-backup.service / systemctl --failed" >&2
  fi
  return 0
}
trap 'backup_failed $? ${LINENO}' ERR

die() {
  # ${BASH_LINENO[0]} is the CALLER's line — the check that tripped is the line an
  # operator has to read, not the body of this function.
  echo "[backup] FAIL: $*" >&2
  backup_failed 1 "${BASH_LINENO[0]:-?}"
  exit 1
}

# --------------------------------------------------------------- config ------
env_from_file() {
  # Last match of KEY= in the repo .env. Returns the value with CR removed and,
  # for an UNQUOTED value, a trailing ` #…` comment cut (that is what
  # `.env.example` ships, e.g. `QDRANT_API_KEY=    # optional` — reading it
  # literally would send a bogus api-key header and a bare `#` in a password
  # would survive). Nothing is printed on the failure paths: this file holds the
  # DB password.
  local key="$1" val=""
  [ -f "${REPO_DIR}/.env" ] || return 0
  val="$(sed -nE "s%^[[:space:]]*${key}[[:space:]]*=[[:space:]]*(.*)%\1%p" "${REPO_DIR}/.env" \
    | tail -n 1)"
  val="${val%$'\r'}"
  case "${val}" in
    \"*\") val="${val#\"}"; val="${val%\"}" ;;
    \'*\') val="${val#\'}"; val="${val%\'}" ;;
    *)      val="${val%%[[:space:]]#*}" ;;
  esac
  # Trailing blanks are never part of these four values.
  val="${val%"${val##*[![:space:]]}"}"
  printf '%s' "${val}"
}

STORAGE_DIR="${STORAGE_DIR:-$(env_from_file STORAGE_DIR)}"
QDRANT_URL="${QDRANT_URL:-$(env_from_file QDRANT_URL)}"
QDRANT_API_KEY="${QDRANT_API_KEY:-$(env_from_file QDRANT_API_KEY)}"

# The old script only ever looked for PG_HOST, which no topology actually sets
# (.env carries DATABASE_URL + POSTGRES_*), so it fell through to
# localhost/postgres and the dump died on credentials. Derive from DATABASE_URL
# when the discrete PG_* vars are absent; explicit env still wins.
_DB_URL="${DATABASE_URL:-$(env_from_file DATABASE_URL)}"
if [ -n "${_DB_URL}" ]; then
  _noscheme="${_DB_URL#*://}"                     # [user:pass@]host[:port]/db[?opts]
  _head="${_noscheme%%\?*}"                       # drop ?options
  _dbname=""
  case "${_head}" in
    */*) _dbname="${_head##*/}"; _head="${_head%/*}" ;;
  esac
  _hostpart="${_head##*@}"
  if [ "${_hostpart}" != "${_head}" ]; then
    _cred="${_head%@*}"
    PG_USER="${PG_USER:-${_cred%%:*}}"
    PGPASSWORD="${PGPASSWORD:-${_cred#*:}}"
  fi
  PG_HOST="${PG_HOST:-${_hostpart%%:*}}"
  case "${_hostpart}" in
    *:*) PG_PORT="${PG_PORT:-${_hostpart##*:}}" ;;
    *)   PG_PORT="${PG_PORT:-5432}" ;;
  esac
  if [ -n "${_dbname}" ]; then PG_DB="${PG_DB:-${_dbname}}"; fi
  unset _DB_URL _noscheme _head _dbname _hostpart _cred
fi
# Percent-encoded characters in a DSN password are NOT decoded here; set
# PGPASSWORD explicitly if your password escapes anything.
PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-postgres}"
PG_DB="${PG_DB:-ai_chat}"
PGPASSWORD="${PGPASSWORD:-postgres}"
export PGPASSWORD

BACKUP_DIR="${BACKUP_ROOT_ENV:-${REPO_DIR}/backups}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
RETAIN_DAYS="${RETAIN_DAYS:-14}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${BACKUP_DIR}/${TS}"

case "${RETAIN_DAYS}" in
  ''|*[!0-9]*) die "RETAIN_DAYS 必须是非负整数（当前：'${RETAIN_DAYS}'）—— 保留期算不出来就绝不能执行本地删除" ;;
esac

# Offsite config, read the same way so the repo .env works as the single config
# source (the units' EnvironmentFile= is only for splitting backup credentials out
# of the app's .env). Exported because sync-offsite.sh is a child process.
BACKUP_RCLONE_REMOTE="${BACKUP_RCLONE_REMOTE:-$(env_from_file BACKUP_RCLONE_REMOTE)}"
BACKUP_RCLONE_PREFIX="${BACKUP_RCLONE_PREFIX:-$(env_from_file BACKUP_RCLONE_PREFIX)}"
RETAIN_REMOTE_DAYS="${RETAIN_REMOTE_DAYS:-$(env_from_file RETAIN_REMOTE_DAYS)}"
RETAIN_REMOTE_DAYS="${RETAIN_REMOTE_DAYS:-30}"
BACKUP_ALERT_URL="${BACKUP_ALERT_URL:-$(env_from_file BACKUP_ALERT_URL)}"
export BACKUP_RCLONE_REMOTE BACKUP_RCLONE_PREFIX RETAIN_REMOTE_DAYS
case "${RETAIN_REMOTE_DAYS}" in
  ''|*[!0-9]*) die "RETAIN_REMOTE_DAYS 必须是非负整数（当前：'${RETAIN_REMOTE_DAYS}'）" ;;
esac
if [ "${RETAIN_REMOTE_DAYS}" -lt "${RETAIN_DAYS}" ]; then
  # Not fatal, but it defeats the point of a longer remote retention: the copy you
  # notice you need on day 20 is the one the remote already dropped.
  echo "[backup] WARN: 异地保留 ${RETAIN_REMOTE_DAYS} 天 < 本地 ${RETAIN_DAYS} 天 —— 异地本该更长，否则等于没有第二道保险" >&2
fi

# 20260101T030000Z -> epoch (GNU date). Keep in sync with scripts/sync-offsite.sh.
ts_to_epoch() {
  local iso
  iso="$(printf '%s' "$1" \
    | sed -E 's%^([0-9]{4})([0-9]{2})([0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})Z$%\1-\2-\3T\4:\5:\6Z%')"
  [ -n "${iso}" ] || return 1
  date -u -d "${iso}" +%s
}

# Qdrant API key support: a 401 used to be swallowed into "no collections".
QDRANT_OPTS=()
if [ -n "${QDRANT_API_KEY}" ]; then QDRANT_OPTS=(-H "api-key: ${QDRANT_API_KEY}"); fi
qc() {
  if [ "${#QDRANT_OPTS[@]}" -gt 0 ]; then curl "${QDRANT_OPTS[@]}" "$@"; else curl "$@"; fi
}

# The collection list is parsed with python; a bare `python` does not exist on
# many production images/venvs, and losing it used to look like "no collections".
PYTHON="${PYTHON_BIN:-}"
if [ -z "${PYTHON}" ]; then
  for _p in python3 python; do
    if command -v "${_p}" >/dev/null 2>&1; then PYTHON="$(command -v "${_p}")"; break; fi
  done
fi
if [ -z "${PYTHON}" ]; then
  die "找不到 python3/python —— 无法解析 Qdrant collection 列表"
fi

mkdir -p "${BACKUP_DIR}"
mkdir -p "${DEST}"
echo "[backup] → ${DEST} (pg ${PG_USER}@${PG_HOST}:${PG_PORT}/${PG_DB}, qdrant ${QDRANT_URL})"

# 1. Postgres logical dump (custom format, parallel-restore-friendly).
if ! pg_dump -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" -d "${PG_DB}" \
     -F c -f "${DEST}/postgres.dump"; then
  die "pg_dump 失败（${PG_USER}@${PG_HOST}:${PG_PORT}/${PG_DB}）"
fi
[ -s "${DEST}/postgres.dump" ] || die "postgres.dump 为空文件"
echo "[backup] postgres OK ($(du -h "${DEST}/postgres.dump" | cut -f1))"

# 2. Qdrant: create a snapshot of every collection, then download the tarball.
#    A failed /collections request is a HARD failure: `|| true` here used to turn
#    "Qdrant is down" into "there is nothing to back up" and exit 0 with zero
#    snapshots.
if ! COLLECTIONS_JSON="$(qc -fsS "${QDRANT_URL}/collections")"; then
  die "取不到 ${QDRANT_URL}/collections —— 拒绝把「读不到」当成「没有集合」"
fi
COLLECTIONS="$(printf '%s' "${COLLECTIONS_JSON}" \
  | "${PYTHON}" -c 'import sys,json; print("\n".join(json.load(sys.stdin)["result"]["collections"].keys()))')"
# The collection list is written next to the snapshots so restore-drill.sh and
# sync-offsite.sh can assert "snapshot count == collection count" instead of
# trusting a glob. One name per line; an empty file means genuinely zero.
if [ -n "${COLLECTIONS}" ]; then
  printf '%s\n' "${COLLECTIONS}" > "${DEST}/qdrant-collections.txt"
else
  : > "${DEST}/qdrant-collections.txt"
fi
EXPECTED_SNAPS="$(grep -c . "${DEST}/qdrant-collections.txt" || true)"
EXPECTED_SNAPS="${EXPECTED_SNAPS:-0}"
SNAP_COUNT=0
for c in ${COLLECTIONS}; do
  if ! qc -fsS -X PUT "${QDRANT_URL}/collections/${c}/snapshots" >/dev/null; then
    die "集合 ${c} 创建快照失败"
  fi
  SNAP="$(qc -fsS "${QDRANT_URL}/collections/${c}/snapshots" \
    | "${PYTHON}" -c 'import sys,json; r=json.load(sys.stdin)["result"]; print(r[-1]["name"] if r else "")')"
  if [ -z "${SNAP}" ]; then
    die "集合 ${c} 建了快照却列不出名字"
  fi
  if ! qc -fsS "${QDRANT_URL}/collections/${c}/snapshots/${SNAP}" -o "${DEST}/qdrant-${c}.snapshot"; then
    die "集合 ${c} 快照下载失败"
  fi
  [ -s "${DEST}/qdrant-${c}.snapshot" ] || die "qdrant-${c}.snapshot 下载后为空文件"
  SNAP_COUNT=$(( SNAP_COUNT + 1 ))
done
if [ "${SNAP_COUNT}" -ne "${EXPECTED_SNAPS}" ]; then
  die "下载到的快照 ${SNAP_COUNT} != 集合数 ${EXPECTED_SNAPS}"
fi
echo "[backup] qdrant OK (${SNAP_COUNT}/${EXPECTED_SNAPS} 个集合快照)"

# 3. Object-storage uploads.
# Respect STORAGE_DIR: under docker-compose the backend writes uploads to
# ${STORAGE_DIR:-/data/uploads}, and the old hardcoded ./backend/data/uploads
# check silently skipped the attachment backup in that topology.
UPLOADS_DIR="${STORAGE_DIR:-./backend/data/uploads}"
if [ -d "${UPLOADS_DIR}" ]; then
  tar -cf "${DEST}/uploads.tar" -C "$(dirname "${UPLOADS_DIR}")" "$(basename "${UPLOADS_DIR}")"
  echo "[backup] uploads OK ($(basename "${UPLOADS_DIR}") -> uploads.tar)"
else
  # Still not a success path. In the compose topology the uploads live in the
  # `uploads_data` NAMED volume, which the host has no path for, so a host-side
  # backup can only produce a partial set. The marker makes the omission part of
  # the batch (sync-offsite.sh refuses an unmarked, uploads-less dir) and stderr
  # carries the workaround.
  echo "[backup] WARN: 上传目录 ${UPLOADS_DIR} 不存在 —— 本批次不含附件，是不完整的备份" >&2
  echo "[backup]       compose 部署请在容器内打包进本目录，然后单独跑一次 sync-offsite.sh：" >&2
  echo "[backup]       docker compose -f docker-compose.prod.yml exec -T backend tar -C /data -cf - uploads > ${DEST}/uploads.tar" >&2
  : > "${DEST}/UPLOADS_SKIPPED"
fi

# 4. Per-batch checksum manifest. restore-drill.sh verifies the extracted uploads
#    tree against MANIFEST.sha256 (a different file, different scope); this one
#    covers the top-level artifacts and is what the offsite push is checked with.
if ! ( cd "${DEST}" \
       && find . -maxdepth 1 -type f ! -name 'SHA256SUMS.txt' -printf '%P\n' \
          | LC_ALL=C sort | xargs -r sha256sum > SHA256SUMS.txt ); then
  die "生成 SHA256SUMS.txt 失败"
fi
[ -s "${DEST}/SHA256SUMS.txt" ] || die "SHA256SUMS.txt 为空 —— 本批次没有任何可校验产物"
if ! ( cd "${DEST}" && sha256sum -c --quiet SHA256SUMS.txt ); then
  die "SHA256SUMS.txt 自校验失败 —— 本地产物已损坏（磁盘或写入问题）"
fi
echo "[backup] manifest OK ($(grep -c . "${DEST}/SHA256SUMS.txt" || true) 个产物文件)"

# 5. Offsite duplication. A bare call on purpose: non-zero propagates through
#    `set -e` into the ERR trap (which alerts) and skips the LOCAL prune below.
#    Not configured => sync-offsite.sh prints "[offsite] SKIP ..." and exits 0,
#    which is a pass-with-warning, never a silent success.
#    Invoked via `bash` because these scripts are tracked as 100644 in the git
#    index (git ls-files -s scripts) — a fresh clone has no exec bit to rely on.
if [ "${BACKUP_SKIP_OFFSITE:-0}" = "1" ]; then
  offsite_state="skipped-by-flag"
  echo "[backup] offsite SKIPPED (BACKUP_SKIP_OFFSITE=1) —— ${TS} 只有本地一份"
elif [ -z "${BACKUP_RCLONE_REMOTE:-}" ] || [ -z "${BACKUP_RCLONE_PREFIX:-}" ]; then
  offsite_state="not-configured"
else
  # sync-offsite.sh prints its own SKIP line and returns 0 when unconfigured; the
  # branches above already cover that, so anything non-zero here aborts the job
  # through `set -e` + the ERR trap, and the prune below is skipped.
  bash "${REPO_DIR}/scripts/sync-offsite.sh" "${DEST}"
  offsite_state="synced-and-verified"
fi

# 6. Prune old LOCAL batches — last, and only this script's own timestamp dirs.
#    The name must match the timestamp grammar (so a stray directory under
#    BACKUP_DIR is never rm -rf'd) and the batch must really be older than
#    RETAIN_DAYS. The previous `find "${BACKUP_DIR}" -maxdepth 1 -type d
#    -mtime +N -exec rm -rf` could also match BACKUP_DIR itself, because maxdepth
#    counts the starting point as depth 0.
_NOW="$(date -u +%s)"
_CUT=$(( RETAIN_DAYS * 86400 ))
pruned_local=0
for d in "${BACKUP_DIR}"/[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z; do
  [ -d "${d}" ] || continue
  n="$(basename "${d}")"
  if [ "${n}" = "${TS}" ]; then
    continue
  fi
  if ! _e="$(ts_to_epoch "${n}")"; then
    echo "[backup] 本地保留：${n} 时间戳无法解析，跳过不删" >&2
    continue
  fi
  if [ $(( _NOW - _e )) -gt "${_CUT}" ]; then
    rm -rf -- "${d}"
    echo "[backup] 本地清理：删除 ${n}（$(( (_NOW - _e) / 86400 )) 天前 > ${RETAIN_DAYS} 天）"
    pruned_local=$(( pruned_local + 1 ))
  fi
done

job_state="done"
case "${offsite_state}" in
  synced-and-verified)
    echo "[backup] done ${TS} —— 本地保留 ${RETAIN_DAYS} 天（已清理 ${pruned_local} 批），异地保留 ${RETAIN_REMOTE_DAYS} 天且已校验"
    ;;
  not-configured)
    echo "[backup] done ${TS} —— 本地保留 ${RETAIN_DAYS} 天；异地副本：没有（未配置 BACKUP_RCLONE_REMOTE/BACKUP_RCLONE_PREFIX）。单副本不算备份，配置见 docs/backup-restore.md"
    ;;
  *)
    echo "[backup] done ${TS} —— 本地保留 ${RETAIN_DAYS} 天；异地副本：本次被 BACKUP_SKIP_OFFSITE=1 跳过，仍是单副本"
    ;;
esac
