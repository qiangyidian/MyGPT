# 备份与恢复（RPO / RTO / 异地同步）

事故里照着读的那一份。部署侧的安装步骤在 [`../deploy/README.md`](../deploy/README.md)，
本文只管四件事：**丢了能丢多少（RPO）**、**多久能恢复（RTO）**、**这份备份能不能用**、
**异地副本怎么配怎么验**。

> 30 秒结论（真实磁盘上的配置，不是理想值）
>
> | 数据 | 最多丢 | 能不能事后重建 |
> |---|---|---|
> | Postgres（账号/会话/消息/计费/文档行/提示词库…） | **约 24 小时**（见 §2.1） | 不能 |
> | 上传原文 `uploads.tar` | **约 24 小时** | **不能**，用户手里的原件除外 |
> | Qdrant 向量 | **约 24 小时** | **能**（向量是 Postgres `document_chunks` 的派生数据，逐文档 reindex） |
> | Redis（队列/限流/黑名单/幂等） | **不进备份**，整卷丢失即全丢；容器重启不丢 | 部分能（见 §2.4，有一处不能，务必读） |
>
> 目标 RTO：**约 1 小时**（§3 的估算合计，尚未按同一台机器实测校准）。
> 前提：异地已配置。没配 rclone 远端时，本机的 14 天目录是**唯一副本**，
> 那就不叫备份，叫等事故。

---

## 1. 一次备份产出什么

`scripts/backup.sh` 每次跑生成一个 UTC 时间戳目录（默认 `<repo>/backups/20260101T030000Z/`）：

| 文件 | 内容 | 缺失意味着 |
|---|---|---|
| `postgres.dump` | `pg_dump -F c` 自定义格式 | 这批不能用 |
| `qdrant-<collection>.snapshot` | 每个 collection 一份快照。collection 命名：知识库 `kb_<id>`、附件共享一份 `chat_attachments` | 快照数 ≠ collection 数即漏备 |
| `qdrant-collections.txt` | 备份当时 `/collections` 返回的集合名，一行一个。**用来核对快照是否齐**，空文件表示确实零集合 | 旧版 backup.sh 产物，无法核对 |
| `uploads.tar` | `${STORAGE_DIR}` 整目录 | 附件没备到 |
| `UPLOADS_SKIPPED` | 上一格的替代标记：目录在宿主机上找不到（compose 命名卷场景），**这批是不完整备份** | — |
| `SHA256SUMS.txt` | 上面所有产物的逐文件 sha256。本地完整性自证（`sha256sum -c`），也是异地推送前的准入门槛；远端一致性另用字节数 + rclone hash 比（§6.2） | 无法校验，`sync-offsite.sh` 拒绝推送 |

保留期是**两套独立数字**：本地 `RETAIN_DAYS`（默认 14 天），异地 `RETAIN_REMOTE_DAYS`
（默认 30 天）。异地故意更长——"上周的备份其实一直是坏的"这种事，往往要过几天才发现。

Redis **不在**这份清单里，Postgres 也**没有** WAL 归档，所以：

- 没有 PITR（按时间点恢复）。能回到的最细粒度是"某一天 03:00 那一刻"。
- 想要 PITR：`archive_mode=on` + `archive_command` + 一份基础备份，属于另一个工程，
  现在没做。（根 README 里"生产建议 `STORAGE_BACKEND=minio`（对象存储自带版本化）"
  这句也不成立——`app/core/storage.py` 里 MinIO/S3 后端直接 raise，落盘只有 local 一条路，
  所以 `uploads.tar` 是附件的唯一副本，异地同步不是加分项而是必需项。）

## 2. RPO：每种存储最多丢多少

### 2.1 Postgres / Qdrant / 上传目录

节奏来自 `deploy/mychat-backup.timer` 的**实际值**：

```
OnCalendar=*-*-* 03:00:00     # 每天一次，本地时区 03:00
AccuracySec=5min              # 最晚 03:05 才起，窗口不是准点
Persistent=true               # 错过了（机器关着）下次开机补跑一次
TimeoutStartSec=2h            # 卡住的 job 会被杀掉，不会和下一轮重叠
```

所以**最坏情况 = 昨天 03:00 之后写入的全部数据 ≈ 24 小时**（外加最多 5 分钟抖动）。
两个会把 RPO 拉得比 24 小时更长的真实情况：

1. **连续多天静默失败**。timer 只会告诉你"上一次跑没跑"，不会告诉你"上一次跑成功没"。
   判活方式在 §5，别只 `systemctl list-timers`。
2. **异地未配置**。`backup.sh` 会打印 `[backup] done … 异地副本：没有`，
   但目录仍然在涨，看起来一切正常——直到那台机器一起没了，RPO 直接变成 100%。

要缩短到 6 小时，改 timer 里注释给的例子（多加一行 `OnCalendar=`，systemd 是"或"关系），
注意每轮都会往异地推一整批全量产物。

### 2.2 上传原文（最痛的那一格）

`uploads.tar` 是原文，且当前只有 local 存储后端，**删了就没了**：Postgres 里
`chat_attachments` / `documents` 行还在，文件不在了，用户看到的是一个能点开、
下载 404 的列表。恢复顺序里它排在最后，但重要性最高。

### 2.3 Qdrant（唯一可以"丢得起"的）

向量是 `document_chunks` 的派生数据。丢了就把每个知识库重新索引
（`POST /api/documents/{document_id}/reindex`，会走 §2.4 的持久摄取队列）。
所以 Qdrant 快照的价值是"省几小时重算 + 省 embedding 费用"，不是"救命"。
反过来说：**别为了救 Qdrant 而推迟起服务**，先把 Postgres 恢复完，向量可以后台补。

### 2.4 Redis：丢的到底是什么

生产 `docker-compose.prod.yml` 的 redis 带 `--appendonly yes` 且挂 `redis_data` 命名卷，
所以**容器重启、宿主机重启不丢**。丢的场景是"卷没了 / 换了机器 / 手滑删了容器"。
Redis 不在备份脚本里，这种场景下全部 Redis 状态从零开始。分四类，后果完全不同
（机制名照代码写，不是印象）：

| Redis 里的东西 | 键 | 丢了会怎样 | 谁来补 |
|---|---|---|---|
| 持久 run 队列 | Stream `agent-run-queue` / group `workers`（`app/agents/workflow/queue.py: RedisStreamQueue`） | **已经**被 worker 领取、正在跑的 run：不丢。租约在 Postgres `run_leases`，`app/recovery.py`（`RecoveryScheduler.scan()`，每 `RECOVERY_SCAN_INTERVAL_SECONDS=30` 秒，单实例靠 `recovery` advisory lock 选主）会把租约过期的 run 置回 `pending` 并 `requeue`，重试预算 `RUN_MAX_RETRIES=3`（数以 `recovery.requeued` 事件计）。"领了消息但还没写租约就死了"的，由同一循环里 leader-gated 的 `RedisStreamQueue.reclaim_stale()`（`XAUTOCLAIM`，空闲阈值 `RUN_STREAM_CLAIM_IDLE_SECONDS=120`）捞回 | ✅ 自动接管/重放 |
| 同上，但 run **从未被领取过**（状态 `pending`、无 `run_leases` 行） | — | **不会自动重放。** `RecoveryScheduler._find_stale_running_no_lease()` 显式排除 `pending`（注释的理由是"它正老老实实在队列里等"），而这条前提在队列本身被清空时不成立：既没有租约可判过期，也没有 stream 条目可 reclaim。用户侧表现是那一轮永远转圈，需要重新发一次消息 | ❌ **没有人补**（`queue.py` 里 "the recovery scheduler re-enqueues it once Redis is back" 那句 docstring 与实现不符，见 §6） |
| 知识库摄取队列 | **不在 Redis。** `Document` 行本身就是队列，调度元数据是 `ingest_*` 列（迁移 `0016_ingestion_queue` / `0019_ingest_claim_version`，`app/services/ingestion_queue.py`），领取靠 `ingest_claim_version` 做所有权证明 | 不受影响：租约 `INGEST_LEASE_TTL_SECONDS=300` 一到，行就重新可领，`INGEST_MAX_ATTEMPTS=4` 次退避重试 | ✅ 自动，且 Redis 全丢也算 |
| 鉴权与成本类旁路状态 | `refresh:blacklist` / `access:blacklist`、限流计数、配额计数、`Idempotency-Key` 去重标记、审批总线 | **安全语义退化**：已登出/已吊销的 refresh token 提前失效期归零（提前变回可用），这是恢复后要立刻知道的一件事；限流与配额计数清零＝超额风险；幂等标记清零＝用户可能重复提交一次 | ❌ 不补，且**不该**补——把黑名单备份回来等于把"撤销"也撤销了 |

写进事故记录里的一句话：**恢复完成后，让所有会话强制重新登录**（吊销黑名单是"丢了更好"
的那类状态，别试图恢复它）。

### 2.5 RPO 一句话版

> 正常一天一备 + 异地已配：Postgres/上传 24h，Qdrant 24h（可重建），Redis 只在整卷丢失时
> 全丢且不自动补 `pending` 从未领取的 run。没配异地：所有数字在机器整机损失时都是 100%。

## 3. RTO：从零恢复一台机器

耗时是**估算**（标了区间的按最慢算），第一次走完请回填真实值到本节——RTO 没实测过
就等于没有。前置条件：你手上有异地副本，怎么配见 §6.1，怎么拉回来见下表步骤 4。

| # | 步骤 | 估算 | 命令 / 判据 |
|---|---|---|---|
| 1 | 机器基线：Docker + compose 插件 + git（宿主原生部署则 python3.12 venv + postgres client + qdrant 二进制，见 `DEPLOY-SERVER.md`） | 10–20 min | 装什么由你的镜像决定；不要在恢复现场才开始读部署文档 |
| 2 | 拉代码到出事时的版本 | 2 min | `git clone && git checkout <出事时的 tag/commit>` |
| 3 | 放 `.env` / `.env.prod`（**含 `FERNET_KEY`**） | 5 min | `FERNET_KEY` 必须和备份时是**同一个**，否则库里所有 API key 密文全部解不开——见 §4 第 9 条 |
| 4 | 从异地拉回选定的那一批时间戳目录 | 5–30 min | `rclone copy mygpt-offsite:backups/mychat-prod/<TS> ./backups/<TS>`（crypt 远端会就地解密） |
| 5 | 校验产物，再决定用不用 | 2 min | `cd backups/<TS> && sha256sum -c SHA256SUMS.txt`，然后跑 §4 的检查单 |
| 6 | 起数据服务，**不起 app** | 3–5 min | `docker compose -f docker-compose.prod.yml --env-file .env.prod up -d postgres redis qdrant` |
| 7 | 灌 Postgres | 3–10 min | 见 §3.1 |
| 8 | 灌 Qdrant | 3–15 min | 见 §3.2（可以先跳过，后台补，见 §2.3） |
| 9 | 解上传目录 | 2–10 min | 见 §3.3 |
| 10 | 跑迁移到 head | 1–3 min | compose 的 `migrate` 一次性服务会自己跑；手工：`docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm migrate` |
| 11 | 起全栈 | 2–5 min | `docker compose -f docker-compose.prod.yml --env-file .env.prod up -d` |
| 12 | 过 `/ready` 门禁 | 1–2 min | 见 §3.4，**这一步不通就不算恢复完** |
| 13 | 演练收尾 | 5–15 min | `./scripts/restore-drill.sh ./backups/<TS>`（隔离容器，不动真库），确认这批产物端到端可用 |
| 14 | 用户可见验证 + 恢复排班记录 | 10 min | 登录、发消息、传附件、开一个知识库问答；记 RTO 实际值 |

合计：**约 50–110 分钟，取 1 小时为目标值**。瓶颈几乎总是步骤 1（拿机器）和步骤 4
（异地带宽），而不是数据库本身。

### 3.1 Postgres

`scripts/restore.sh` 用的是 `pg_restore --clean --if-exists --no-owner -j 4`，
它会 **DROP 再 CREATE**——确认你连的是要恢复的那个库。

```bash
# 空库直接 pg_restore（脚本的第一段就是它）
PGPASSWORD=... pg_restore -h 127.0.0.1 -p 5432 -U <user> -d ai_chat \
  --clean --if-exists --no-owner -j 4 backups/<TS>/postgres.dump
```

`--clean` 在空库上会吐一堆 "table does not exist" 提示，**那是噪音不是失败**；
真失败的判据是 `pg_restore` 退出码非零且报的是 dump 本身读不下去。

### 3.2 Qdrant

快照恢复会**创建** collection，所以目标必须没有同名集合：

```bash
curl -fsS -X DELETE "$QDRANT_URL/collections/<name>"   # 有同名就先删，否则 upload 会报错
curl -fsS -X PUT "$QDRANT_URL/collections/<name>/snapshots/upload" \
  -H "Content-Type: multipart/form-data" -F "file=@backups/<TS>/qdrant-<name>.snapshot"
```

恢复了几个集合要和 `qdrant-collections.txt` 的行数对上，不是和 `ls` 对上。
带 `QDRANT_API_KEY` 时每条 curl 都要加 `-H "api-key: ..."`。

### 3.3 上传目录

`scripts/restore.sh` 的第三段把 tar 解到 **`./backend/data`（相对当前目录，硬编码）**，
它不读 `STORAGE_DIR`。所以：

```bash
cd /root/MyGPT                                  # 只在这台机器就用默认路径时才有效
tar -tf backups/<TS>/uploads.tar                # 先看顶层目录名是什么
# 宿主原生部署（DEPLOY-SERVER.md）：按实际 STORAGE_DIR 解，别用脚本第三段
tar -xf backups/<TS>/uploads.tar -C "$(dirname /opt/mychat-data/uploads)"
# compose 部署：卷是 uploads_data，宿主机没有对应目录，只能灌进卷
docker compose -f docker-compose.prod.yml cp backups/<TS>/uploads.tar backend:/tmp/uploads.tar
docker compose -f docker-compose.prod.yml exec backend sh -c 'mkdir -p /data && tar -xf /tmp/uploads.tar -C /data'
```

解完抽样确认权限/属主能被 backend 进程读到（`/ready` 的 `storage` 项就是探这个）。

### 3.4 `/ready` 门禁（为什么"起来了"不等于"能服务"）

`GET /ready`（`backend/app/core/health.py`）七个分项**全绿才 200**：
`db`、`db_migration`、`redis`、`qdrant`、`storage`、`runner`、`chat_model`。

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/ready   # compose
curl -s http://127.0.0.1:8003/ready | python3 -m json.tool             # 宿主原生，看哪一项红
```

恢复现场最常踩的是 `db_migration`：它要求 **DB 的 alembic revision 与仓库迁移图一致**。
head 从 `backend/migrations/versions/` 动态解析；文档不记录具体 revision，避免新增迁移后
操作指引过期。用 `cd backend && alembic heads` 查看当前唯一 head，用
`alembic current` 查看目标数据库版本。判断方法：

- `db_migration` 红 + `reason` 里写 `behind` → 迁移没跑完，回步骤 10；
- 写 `ahead` → **允许**（代码回滚到升级过的库，expand-contract 语义），不是故障；
- 写 `unknown` → 库里的 `version_num` 不在本仓库链条里（从别处搬来的库），停下来查，
  不要 `alembic stamp` 硬盖。

`runner` 红通常是宿主没给容器 docker socket；`chat_model` 红是库里没有可用的非 embedding
模型配置（`ModelConfig`）——这两项和备份无关，但它们会让你误判"备份坏了"。

## 4. 什么情况下**不该**用这份备份

按顺序过，任何一条命中就换更早（或更新）的一批，并把它记为事故样本而不是"运气不好"：

1. **`SHA256SUMS.txt` 校验不过**，或者根本没有这个文件。`sha256sum -c` 报错 = 产物被截断
   或磁盘坏了，直接放弃这一批。
2. **没有 `postgres.dump` 或它是 0 字节**。
3. **`UPLOADS_SKIPPED` 存在，或没有 `uploads.tar`**。这批只能用一次，并且要立刻通知
   出事时间段里上传过文件的用户——他们的附件回不来。
4. **`qdrant-*.snapshot` 数量 ≠ `qdrant-collections.txt` 行数**。说明备份当时 Qdrant
   只回了一半（或快照下载被吞掉了）。向量可以重建，所以这条**不阻塞起服务**，
   但要知道有一批知识库的索引是旧的。若 `qdrant-collections.txt` 是空文件，
   那是"当时真没集合"；若该文件不存在，那是旧版 `backup.sh` 的产物，无法核对——
   把"能不能核对"当成一个备份格式版本问题来对待。
5. **时间戳目录不完整**：目录在、但只有部分文件（例如上一次异地/本地清理被中断）。
   一律不用。清理只在"这一批成功推送到异地之后"才发生，所以一个残缺目录通常是
   中途被 SIGKILL（`TimeoutStartSec=2h`）留下的。
6. **最近一次 `restore-drill.sh` 是 FAIL**，或者根本没人记得跑过。未演练的备份不算备份，
   这是本节存在的理由。
7. **备份批次的时间戳晚于你要回滚到的时刻**（想回到 T，结果只有 T+1 的备份）——
   拿它恢复等于把用户数据往前抹，比故障本身更糟。
8. **异地和本地是同一台机器/同一个卷的快照**。配置写错时"异地"其实就在旁边，
   `rclone check` 照样绿。用 `rclone lsd <remote>:` 确认它真的在另一个地方。
9. **`FERNET_KEY` 换了**。库能起、页面能登，但所有 provider API key 解不开，
   表现为模型全部报错。恢复时先比对该 key 与备份时间是否一致。

## 5. 备份失败要有人看得见

三条通道，按可信度排：

1. **journal（一定有）**：`journalctl -u mychat-backup.service -n 200 --no-pager`；
   排班看板判活用 `systemctl --failed | grep mychat-backup`，**不要**看
   `systemctl list-timers`——它只证明 timer 排上了，不证明上一次跑成功。
2. **`BACKUP_ALERT_URL`（要显式配）**：`scripts/backup.sh` 里每个失败出口都会走
   `backup_failed()`，往这个 webhook 推一条中文消息。**它就是
   `deploy/monitoring/alertmanager.yml` 里 `ops-critical` 用的那类企业微信/钉钉
   robot webhook**，不新发明通道。没配置时脚本会在 stderr 明确说"本次失败无人被通知"，
   而不是把没配置当成"已经通知过了"。
3. **`OnFailure=mychat-backup-alert.service`（覆盖 trap 覆盖不到的情况）**：
   job 被 `TimeoutStartSec` 或 OOM 直接 SIGKILL 时，bash 的 ERR trap 来不及跑。
   这个单元用 `logger` 写一条独立 journal 记录，并在 `BACKUP_ALERT_URL` 已配置时
   额外 POST 一次；它自己不挂 `OnFailure`，不递归。

> **Alertmanager 看不到备份**：Prometheus 只抓 API 的 `/metrics`
> （`deploy/monitoring/prometheus.yml`），`deploy/monitoring/rules/mygpt.yml` 里
> 没有任何 backup 规则。别在告警平台上等一条永远不会响的规则。要接进去得先做一个
> 能被抓取指标的 exporter，那是另一件事。

## 6. 异地同步（`scripts/sync-offsite.sh`）

### 6.1 配置

```bash
# /etc/mychat/backup.env（mychat-backup.service 与 -alert.service 共用；chmod 600）
BACKUP_RCLONE_REMOTE=mygpt-offsite
BACKUP_RCLONE_PREFIX=backups/mychat-prod      # 远端里一个**专属目录**，不要给桶根
RETAIN_REMOTE_DAYS=30                          # 必须 >= 本地 RETAIN_DAYS
BACKUP_ALERT_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<REPLACE-ME>
```

远端必须是 rclone 的 **`crypt`** 类型（`rclone config` → type=crypt → 包住真正的
s3/sftp/b2 后端）。原因写在脚本头上：备份里有**用户上传原文**和 **provider API key 的
Fernet 密文**，把明文目录扔进一个你没设 ACL 的公开桶，是数据泄露事件，不是备份。
脚本会读 `rclone config dump` 判断类型，**不是 crypt 就直接失败退出**；
确实需要推到明文远端时，显式设 `BACKUP_ALLOW_PLAINTEXT_REMOTE=1` 承认风险才继续。

### 6.2 失败语义（这是"工程级"的部分）

- 两个变量任一未配 → 打印一行 `[offsite] SKIP: 未配置 …` 并 exit 0，
  `backup.sh` 收尾再重复一次"异地副本：没有"。**跳过被说成跳过，不会被说成成功。**
- 已配置但出问题（没有 rclone 二进制、没有 python、远端不可达、推过去的文件远端缺、
  字节数不符、hash 不符）→ **非零退出** → `backup.sh` 的 `set -e` + ERR trap：
  整个备份 job 判失败、告警、**并且本地清理不会执行**。顺序是刻意的：
  一批从没出过门的产物，不该顺手把家里旧的也删掉。
- 推送前还会拒绝不合格的这一批：`postgres.dump` 缺失/空、`SHA256SUMS.txt` 缺失、
  既无 `uploads.tar` 也无 `UPLOADS_SKIPPED` 标记、快照数 ≠ collection 数、
  本地 sha256 自校验不过。
- 推送后**两路独立校验**：① 远端 `lsjson` 的逐文件**字节数**和本地一致
  （截断/漏传一次往返就抓到）；② `rclone check --one-way` 比**内容 hash**。
  任一有条目缺失即 exit 1。
- 只传当前这一批：`rclone copy <本地时间戳目录> <remote>/<prefix>/<TS>`，
  用 `copy` 不用 `sync`（`sync` 有权删远端它认为多余的东西，本脚本里唯一的删除是下面那条）。

### 6.3 为什么清理不能一把 `rclone delete` 整个远端

远端保留期是**逐目录、按名字里的时间戳算年龄**，并且：

- 只在 `BACKUP_RCLONE_PREFIX` 下面动（前缀以 `/` 开头或含 `..` 直接拒绝执行）；
- 目录名必须严格匹配 `YYYYMMDDTHHMMSSZ`——别人的目录、手放进去的临时副本、
  名字解析不出来的东西，一律**保留**；
- 宁可少删：删多了是数据丢失，少删只是存储费。

而 `rclone delete remote:` / `rclone purge remote:bucket` 是递归且不可撤销的，
同一个桶里可能有别人的服务、别的租户的数据。把它写进脚本，等于把"某次有人改了前缀"
变成一次删库。

### 6.4 已知缺陷（照读时请当作前提，不是意外）

- **`queue.py` 里 `RunQueueUnavailable` 的 docstring 与实现不符**：它承诺
  "run 行留在 `pending`，Redis 恢复后由 recovery 重新入队"，但
  `RecoveryScheduler._find_stale_running_no_lease()` 明确把 `pending` 排除在
  扫描之外，`_find_expired_leases()` 又要求存在租约行 → 从未被领取过的 `pending`
  run 在队列数据丢失后没有任何自动重放路径。文档 §2.4 按实现写。
  修法属于引擎侧，不在本文范围。
- **compose 拓扑下宿主机看不到上传文件**（`uploads_data` 是命名卷），
  所以宿主侧的 `backup.sh` 只能产出带 `UPLOADS_SKIPPED` 的部分备份。
  要么按 §3.3/`backup.sh` 提示的那条 `docker compose exec … tar` 补上传物，
  要么把卷改成 bind mount。**当前 `mychat-backup.service` 已经不再硬编码
  `STORAGE_DIR=/data/uploads`**（那个值在宿主上是错的），改成从 `.env` 读。
- **没有 PITR**、**Redis 不备份**、**`MANIFEST.sha256`（附件逐文件清单）没有任何脚本生成**
  （`restore-drill.sh` 支持读它，但 `backup.sh` 从来没写过，所以演练里那条分支实际不会走，
  走的是 tar 往返校验）。
- `.gitignore` 里没有 `backups/`，而备份目录含用户上传原文——把仓库目录选成
  `BACKUP_DIR` 时有一次 `git add .` 就泄露一次。要么显式加忽略，要么把
  `BACKUP_DIR` 指到仓库外。

## 7. 恢复演练：`scripts/restore-drill.sh`

```bash
./scripts/restore-drill.sh ./backups/<TS>
PG_PORT=55433 QDRANT_PORT=6334 ./scripts/restore-drill.sh ./backups/<TS>
```

它**起一次性隔离容器**（`postgres:16-alpine` + `qdrant/qdrant:v1.12.4`，绑 `127.0.0.1`，
端口默认 55433/6334，跑完 `trap` 删容器），**绝不写真实库**。实际验的是这四件事：

0. 产物集合自检（起容器之前）：`SHA256SUMS.txt` 逐文件 sha256 能否验过、
   `qdrant-*.snapshot` 数是否等于 `qdrant-collections.txt` 行数、
   有没有 `UPLOADS_SKIPPED` 标记（有则 WARN）。
1. Postgres：dump 能否恢复，且恢复出来的 `alembic current` **等于仓库动态解析的 head**
   （证明迁移修订号能活着走过一次备份/恢复）。
2. Qdrant：每个快照能否 upload 成功、集合是否被列出来（数量 ≥ 快照数）。
3. 附件：`uploads.tar` 能解开，且解两遍逐文件 sha256 一致（tar 往返完整性）；
   若目录带 `MANIFEST.sha256` 则改为按该清单校验。

退出码 `0` = `[drill] RESULT: PASS`；任一失败是 `1` + 具体是哪一项。

**建议频率**：随每次真实恢复动作，即至少**每月一次**，外加
(a) 每次改了 `backup.sh` / `restore*.sh` / 迁移链之后、(b) 改了异地远端或保留期之后、
(c) 事故复盘之后。演练失败按 P2 起处理——它意味着你**当下没有可用备份**，
而不是"测试挂了"。

它**不验**的：端到端业务可用性（脚本末尾自己提示：想验就在备份前插一条探针
artifact、恢复后查它）、异地链路（异地链路由 `sync-offsite.sh` 的推送后校验负责）、
`/ready` 的 `runner` / `chat_model` 项。这三块要有人在真实恢复演练（§3）里手工过。

---

### 相关位置

- 安装/排期：[`../deploy/README.md`](../deploy/README.md)（备份单元的安装步骤）
- 服务器真实拓扑：[`../DEPLOY-SERVER.md`](../DEPLOY-SERVER.md)（宿主原生：PG/Redis/Qdrant
  为系统服务，上传在 `/opt/mychat-data/uploads`，后端 `127.0.0.1:8003`）
- compose 拓扑：`docker-compose.prod.yml`（命名卷 `postgres_data` / `redis_data` /
  `qdrant_data` / `uploads_data`，`.env.prod` 约定见文件头）
- 脚本：`scripts/backup.sh`、`scripts/sync-offsite.sh`、`scripts/restore.sh`、
  `scripts/restore-drill.sh`、`scripts/verify_migrations.sh`
- 单元：`deploy/mychat-backup.{service,timer}`、`deploy/mychat-backup-alert.service`
