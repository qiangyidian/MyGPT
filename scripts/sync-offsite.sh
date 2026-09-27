#!/usr/bin/env bash
# MyGPT offsite backup duplication — push ONE timestamped backup dir with rclone.
#
# Called by scripts/backup.sh once the local artifacts are complete, and safe to
# run by hand for a dir that is already on disk:
#   ./scripts/sync-offsite.sh ./backups/20260101T030000Z
#
# FAILURE SEMANTICS (this is the reason the script exists — a backup that only
# lives on the machine that burns down is not a backup):
#   * remote NOT configured (BACKUP_RCLONE_REMOTE or BACKUP_RCLONE_PREFIX empty)
#       -> prints one explicit "[offsite] SKIP ..." line and exits 0. Nothing left
#          the building, and backup.sh's summary repeats it. Skipping is loud by
#          contract; it is never reported as a successful sync.
#   * remote configured but anything is wrong (no rclone binary, unreachable
#     remote, a file missing remotely, a size/hash mismatch)
#       -> exits non-zero. backup.sh runs under `set -e`, so the whole backup job
#          fails, the local prune is skipped, and the ERR trap in backup.sh emits
#          the alert. A partially-pushed batch is still on the remote: the next
#          successful run pushes a new timestamp dir, and prune only ever removes
#          dirs strictly older than RETAIN_REMOTE_DAYS, so a half-written dir is
#          never treated as a restorable one (see docs/backup-restore.md, "什么
#          情况下不该用这份备份").
#
# ENCRYPTION — READ THIS BEFORE CONFIGURING THE REMOTE:
#   A backup contains every uploaded file verbatim (uploads.tar) plus Fernet
#   ciphertexts of provider API keys. The remote named in BACKUP_RCLONE_REMOTE
#   MUST be an rclone `crypt` remote (rclone config type=crypt wrapping the real
#   s3/sftp/b2 backend). Dropping the artifacts into a plain public bucket, or a
#   bucket whose ACL you did not set, is a data leak, not a backup.
#   sync-offsite.sh refuses to run against an obviously-plaintext remote when
#   BACKUP_ALLOW_PLAINTEXT_REMOTE is unset (see check below).
#
# Env:
#   BACKUP_RCLONE_REMOTE    rclone remote name, e.g. mygpt-offsite      (required)
#   BACKUP_RCLONE_PREFIX    dedicated dir under it, e.g. backups/mychat-prod (required)
#   RETAIN_REMOTE_DAYS      remote retention, default 30 — keep it >= local RETAIN_DAYS
#   RCLONE_BIN              default: rclone
#   RCLONE_COPY_OPTS        extra flags for the copy step, e.g. "--transfers 4 --bwlimit 20M"
#   BACKUP_ALLOW_PLAINTEXT_REMOTE=1  acknowledge a non-crypt remote and proceed
set -euo pipefail

REMOTE="${BACKUP_RCLONE_REMOTE:-}"
PREFIX="${BACKUP_RCLONE_PREFIX:-}"
RETAIN_REMOTE_DAYS="${RETAIN_REMOTE_DAYS:-30}"
RCLONE="${RCLONE_BIN:-rclone}"
PYTHON="${PYTHON_BIN:-}"

die() { echo "[offsite] FAIL: $*" >&2; exit 1; }

SRC="${1:-}"
if [ -z "${SRC}" ] || [ ! -d "${SRC}" ]; then
  echo "usage: $0 <backup-dir>  (e.g. ./backups/20260101T030000Z)" >&2
  exit 2
fi
SRC="$(cd "${SRC}" && pwd -P)"
NAME="$(basename "${SRC}")"
DEST="${REMOTE}:${PREFIX%/}/${NAME}"

if [ -z "${PYTHON}" ]; then
  for p in python3 python; do
    if command -v "${p}" >/dev/null 2>&1; then PYTHON="$(command -v "${p}")"; break; fi
  done
fi

# --- remote dir name is our own format; nothing else is ever deleted ----------
# Same timestamp grammar backup.sh writes. Used as a whitelist for the prune:
# an unparseable or foreign directory under the prefix is left alone, always.
TS_RE='^[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z$'

# 20260101T030000Z -> epoch seconds (GNU date). Non-zero on a name we cannot
# parse, so a caller can never delete on a bad guess.
ts_to_epoch() {
  local iso
  iso="$(printf '%s' "$1" \
    | sed -E 's%^([0-9]{4})([0-9]{2})([0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})Z$%\1-\2-\3T\4:\5:\6Z%')"
  [ -n "${iso}" ] || return 1
  date -u -d "${iso}" +%s
}

# --- not configured: skip out loud, never silently ----------------------------
if [ -z "${REMOTE}" ] || [ -z "${PREFIX}" ]; then
  echo "[offsite] SKIP: 未配置 BACKUP_RCLONE_REMOTE / BACKUP_RCLONE_PREFIX —— ${NAME} 只存在本机一份，这不构成异地备份（配置方法见 docs/backup-restore.md）"
  exit 0
fi

command -v "${RCLONE}" >/dev/null 2>&1 \
  || die "已配置异地远端 ${REMOTE}，但找不到 rclone 可执行文件（${RCLONE}）——不能当成同步成功"
# The remote listing is parsed below, so the verification step is not optional.
[ -n "${PYTHON}" ] \
  || die "已配置异地远端，但找不到 python3/python —— 无法解析远端清单，推送后校验做不了"

# PREFIX must be a real sub-directory of the remote, never the bucket root: the
# prune below deletes under it, and a root prefix would put every other tenant
# of that bucket in scope.
case "${PREFIX}" in
  /*) die "BACKUP_RCLONE_PREFIX 不能以 / 开头（会得到 // 这种病态路径）" ;;
  *..*) die "BACKUP_RCLONE_PREFIX 不能包含 ..（越出前缀就等于越出备份目录）" ;;
esac

# The remote must be a crypt remote (see the header). `rclone listremotes` plus
# the config file tells us the type; if we cannot determine it we only warn, but
# a plaintext type is a hard error unless explicitly acknowledged.
REMOTE_TYPE="$("${RCLONE}" config dump 2>/dev/null | "${PYTHON}" -c '
import json,sys
try:
    name = sys.argv[1]
    print(json.load(sys.stdin).get(name, {}).get("type", ""))
except Exception:
    print("")
' "${REMOTE}" 2>/dev/null || true)"
if [ "${REMOTE_TYPE}" = "crypt" ]; then
  echo "[offsite] remote ${REMOTE} type=crypt（异地副本已加密）"
elif [ "${BACKUP_ALLOW_PLAINTEXT_REMOTE:-0}" = "1" ]; then
  echo "[offsite] WARN: 远端 ${REMOTE}${REMOTE_TYPE:+ type=${REMOTE_TYPE}} 不是 crypt —— 异地副本是明文，含用户上传原文与 API key 密文" >&2
else
  die "远端 ${REMOTE}${REMOTE_TYPE:+ type=${REMOTE_TYPE}} 不是 rclone crypt 类型。备份里含用户上传原文与 API key 密文，明文落地异地等于数据泄露；确认远端已用 crypt 包装后设 BACKUP_ALLOW_PLAINTEXT_REMOTE=1 才会继续"
fi

# --- refuse to push an incomplete batch --------------------------------------
# The local half of backup.sh already failed loudly on these; re-checking here
# means a hand-run sync cannot push a postgres-less or uploads-less dir and call
# it a backup.
[ -s "${SRC}/postgres.dump" ] || die "${SRC}/postgres.dump 缺失或为空 —— 拒绝推送不完整的备份批次"
[ -s "${SRC}/SHA256SUMS.txt" ] || die "${SRC}/SHA256SUMS.txt 缺失 —— 无法做推送后校验，拒绝推送"
if [ ! -s "${SRC}/uploads.tar" ] && [ ! -f "${SRC}/UPLOADS_SKIPPED" ]; then
  die "${SRC} 既没有 uploads.tar 也没有 UPLOADS_SKIPPED 标记 —— 备份批次来源不明，拒绝推送"
fi
if [ ! -f "${SRC}/qdrant-collections.txt" ]; then
  echo "[offsite] WARN: 缺少 qdrant-collections.txt（旧版 backup.sh 产物），无法核对快照数量" >&2
else
  EXPECTED_SNAPS="$(grep -c . "${SRC}/qdrant-collections.txt" || true)"
  EXPECTED_SNAPS="${EXPECTED_SNAPS:-0}"
  ACTUAL_SNAPS="$(find "${SRC}" -maxdepth 1 -type f -name 'qdrant-*.snapshot' | wc -l | tr -d ' ')"
  [ "${ACTUAL_SNAPS}" -eq "${EXPECTED_SNAPS}" ] \
    || die "Qdrant 快照数 ${ACTUAL_SNAPS} != collection 数 ${EXPECTED_SNAPS} —— 这份批次不可信，拒绝推送"
fi
( cd "${SRC}" && sha256sum -c --quiet SHA256SUMS.txt ) \
  || die "本地 SHA256SUMS.txt 自校验失败 —— 本地产物本身已损坏，先查磁盘再谈异地"

# --- push only this run's timestamp dir --------------------------------------
# `copy`, never `sync`: copy can add files, sync can delete remote files it
# decides are surplus. The only deletion in this script is the retention prune
# below, which is guarded by the timestamp whitelist.
echo "[offsite] push ${SRC} -> ${DEST}（仅本次时间戳目录）"
# shellcheck disable=SC2086
"${RCLONE}" copy "${SRC}" "${DEST}" ${RCLONE_COPY_OPTS:-}

# --- post-push verification --------------------------------------------------
# Two independent signals; either failing aborts the job non-zero:
#   1. every local file exists remotely with the SAME byte size (a truncated or
#      never-transferred object is caught here, cheaply, in one round trip);
#   2. rclone check compares content hashes one-way (source -> destination),
#      catching same-size-but-different-bytes and files missing remotely.
echo "[offsite] 校验远端清单与字节数"
# Artifact names are generated by backup.sh (postgres.dump / qdrant-<coll>.snapshot
# / uploads.tar / SHA256SUMS.txt / qdrant-collections.txt) and carry no whitespace,
# so "path size" one-line-per-file is an unambiguous comparison format.
LOCAL_LIST="$(cd "${SRC}" && find . -maxdepth 1 -type f -printf '%P %s\n' | LC_ALL=C sort)"
# A listing failure is a verification failure, not an empty remote.
REMOTE_JSON="$("${RCLONE}" lsjson --files-only -R "${DEST}")" \
  || die "推送后列不出远端目录 ${DEST} —— 这次同步没有建立起来，判为失败"
REMOTE_LIST="$(printf '%s' "${REMOTE_JSON}" | "${PYTHON}" -c '
import json, sys
for obj in json.load(sys.stdin):
    print("%s %s" % (obj.get("Path") or obj.get("path"), obj.get("Size") or obj.get("size")))
' | LC_ALL=C sort)"

missing_or_diff=""
while read -r f size; do
  [ -n "${f}" ] || continue
  # Literal field match (no regex): filenames come from the local listing.
  remote_size="$(printf '%s\n' "${REMOTE_LIST}" | awk -v want="${f}" '$1 == want { print $2; exit }')"
  if [ -z "${remote_size}" ]; then
    missing_or_diff="${missing_or_diff}  远端缺失: ${f}\n"
  elif [ "${remote_size}" != "${size}" ]; then
    missing_or_diff="${missing_or_diff}  字节数不符: ${f} (本地 ${size} / 远端 ${remote_size})\n"
  fi
done <<EOF
${LOCAL_LIST}
EOF

if [ -n "${missing_or_diff}" ]; then
  printf '[offsite] FAIL: 推送后校验未通过：\n%b' "${missing_or_diff}" >&2
  printf '[offsite]       远端 %s —— 整个备份 job 判为失败，本地副本不会被清理\n' "${DEST}" >&2
  exit 1
fi

# Hash-level check. --one-way means "destination must be an up-to-date copy of
# the source"; extra files on the remote are not an error here (they can only be
# leftovers of an interrupted run, which the prune removes by age).
"${RCLONE}" check "${SRC}" "${DEST}" --one-way \
  || die "rclone check 失败：远端 ${DEST} 与本地产物内容不一致"

SYNCED_BYTES="$(printf '%s\n' "${LOCAL_LIST}" | awk '{s+=$2} END {printf "%.1f MB", (s+0)/1048576}')"
echo "[offsite] OK: ${NAME} 已加密推送并校验通过（${SYNCED_BYTES}，$(printf '%s\n' "${LOCAL_LIST}" | grep -c . || true) 个文件）"

# --- remote retention: separate from the local one ---------------------------
# Why this cannot be `rclone delete ${REMOTE}:${PREFIX}` (let alone the remote
# root):
#   * the remote may hold other tenants' data and other services' backups under
#     the same bucket; rclone delete/purge is recursive and has no undo;
#   * even inside our own prefix, a name that is not a backup timestamp may be a
#     manual copy an operator dropped there;
#   * the retention decision must be per-directory and age-based, so a directory
#     whose name we cannot parse is NEVER deleted — failing to delete is a
#     storage-cost bug, deleting the wrong thing is a data-loss bug.
# Remote retention is intentionally LONGER than local (default 30 vs 14 days) so
# "we only noticed on Monday that last week's backups were broken" still has a
# copy to go back to.
echo "[offsite] 清理远端（仅 ${REMOTE}:${PREFIX%/}/ 下、名字符合时间戳格式、且确实老于 ${RETAIN_REMOTE_DAYS} 天的目录）"
case "${RETAIN_REMOTE_DAYS}" in
  ''|*[!0-9]*) die "RETAIN_REMOTE_DAYS 必须是非负整数（当前：'${RETAIN_REMOTE_DAYS}'）—— 保留期算不出来就绝不能执行删除" ;;
esac
NOW="$(date -u +%s)"
CUTOFF_SECONDS=$(( RETAIN_REMOTE_DAYS * 86400 ))
# A failed listing must not read as "nothing to delete": keep going (the artifacts
# are already safe offsite, which is the part that matters) but say out loud that
# retention did not run, so the remote does not silently grow forever.
if ! REMOTE_DIRS="$("${RCLONE}" lsf --dirs-only "${REMOTE}:${PREFIX%/}/")"; then
  echo "[offsite] WARN: 列不出远端目录 ${REMOTE}:${PREFIX%/}/ —— 本次异地保留期清理未执行，异地副本可能继续膨胀" >&2
  REMOTE_DIRS=""
fi
pruned=0
while read -r d; do
  d="${d%/}"
  [ -n "${d}" ] || continue
  if ! printf '%s' "${d}" | grep -qE "${TS_RE}"; then
    echo "[offsite] 保留（非备份时间戳目录，绝不删除）: ${d}"
    continue
  fi
  if [ "${d}" = "${NAME}" ]; then
    continue  # the batch we just pushed
  fi
  if ! EPOCH="$(ts_to_epoch "${d}")"; then
    echo "[offsite] 保留（时间戳无法解析，宁可不删）: ${d}" >&2
    continue
  fi
  if [ $(( NOW - EPOCH )) -gt "${CUTOFF_SECONDS}" ]; then
    # Scoped to exactly one whitelisted directory under our own prefix.
    "${RCLONE}" purge "${REMOTE}:${PREFIX%/}/${d}"
    echo "[offsite] 已删除远端 ${d}（$(( (NOW - EPOCH) / 86400 )) 天前 > ${RETAIN_REMOTE_DAYS} 天保留期）"
    pruned=$(( pruned + 1 ))
  fi
done <<EOF
${REMOTE_DIRS}
EOF
echo "[offsite] 远端清理完成（删除 ${pruned} 个过期批次；本地保留期见 RETAIN_DAYS）"
