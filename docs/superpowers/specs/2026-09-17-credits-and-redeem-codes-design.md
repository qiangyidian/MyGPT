# 积分与兑换码 — 设计文档

日期：2026-09-17
状态：已批准（实现中）

## 1. 目标与范围

平台接入**预付费积分**：管理员生成兑换码，用户输入兑换码获得积分，积分按实际模型消耗逐轮扣减。不引入任何支付通道。

本设计覆盖：

- 积分账户与不可篡改的流水账本
- 兑换码批次管理（生成 / 导出 / 作废 / 核销记录）
- 用户侧兑换与余额查询
- 管理员手动调分
- 扣分接入现有聊天 / Agent 链路
- 余额不足拦截（带观察模式开关）

明确不做（YAGNI）：

- 支付通道、退款、发票
- 积分过期、积分转赠、积分商城
- 通用码 / 限次码（每码一次性，见 §3.3）
- 按功能门禁扣分（只做按量扣分）

## 2. 现状与缺口

代码库中**不存在任何积分概念**。存在的只是一个管理员配置的服务端上限体系：

| 位置 | 现状 |
|---|---|
| `backend/app/quotas.py` | 多维配额（并发/token/成本/存储/连接器/工具），`QUOTAS_ENABLED` 默认关闭，Redis 有内存兜底 |
| `backend/app/models/user.py` | 无余额字段 |
| `backend/app/api/admin.py` | 只有用户列表、系统状态、审计日志 |
| 前端 | 无任何余额 / 用量 / 兑换界面 |

配额是"上限"，不是"余额"：它不随用户充值增长，没有流水，没有审计轨迹，用户看不到。二者不冲突，积分**叠加**在配额之上，各自独立开关。

### 2.1 必须一并修复的既有缺陷

`backend/app/services/chat_service.py` 中有 5 处调用 `_apply_usage_accounting`，但只有 3 处调用 `_charge_quota_if_enabled`：

| 行 | 函数 | 记账 | 计费 |
|---|---|---|---|
| 1754 / 1759 | `ChatService._run`（内联流式） | ✅ | ✅ |
| 1911 | `_finalize_error`（错误收尾） | ✅ | ❌ |
| 1928 | `_finalize_interrupted` | ✅ | ❌ |
| 2504 / 2509 | `run_durable_turn`（done，worker） | ✅ | ✅ |
| 2537 / 2541 | `run_durable_turn`（CancelledError） | ✅ | ✅ |

`_finalize_error` 消费了 token 却从不计费。今天这是配额旁路；在预付费模型下这是**免费刷额度漏洞** —— 故意让请求报错就能白烧 token。

修复方式见 §5.1：把记账与计费合并成单一入口，物理上不可能只记账不计费。

## 3. 数据模型

四张新表。所有余额变动都在**同一事务**内完成：锁账户行 → 写流水 → 改余额。

### 3.1 `credit_accounts` — 每人一行，权威余额

| 列 | 类型 | 说明 |
|---|---|---|
| `user_id` | UUID PK, FK users.id ON DELETE CASCADE | |
| `balance` | BIGINT NOT NULL DEFAULT 0 | 权威余额，可为负 |
| `lifetime_granted` | BIGINT NOT NULL DEFAULT 0 | 累计发放 |
| `lifetime_consumed` | BIGINT NOT NULL DEFAULT 0 | 累计消耗 |
| `created_at` / `updated_at` | | |

`balance` 不设 `CHECK (balance >= 0)`，理由见 §4.3。

### 3.2 `credit_ledger` — 只追加，永不修改

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | |
| `user_id` | UUID FK ON DELETE CASCADE, index | |
| `delta` | BIGINT NOT NULL | 正=发放，负=消耗 |
| `balance_after` | BIGINT NOT NULL | 写时快照，用于对账与展示 |
| `reason` | VARCHAR(32) | `redeem` / `admin_adjust` / `usage` / `signup_bonus` |
| `ref_type` | VARCHAR(32) NULL | `redeem_code` / `message` / `admin` |
| `ref_id` | VARCHAR(64) NULL | |
| `actor_id` | UUID NULL FK users.id ON DELETE SET NULL | 操作管理员；系统扣费为 NULL |
| `note` | TEXT NULL | |
| `created_at` | TIMESTAMPTZ NOT NULL | |

索引：`(user_id, created_at DESC)`。外加：

```sql
CREATE UNIQUE INDEX uq_credit_ledger_ref
  ON credit_ledger (ref_type, ref_id, reason)
  WHERE ref_type IS NOT NULL;
```

**这是幂等的核心。** 同一轮对话（`ref_type='message'`）只能产生一条 `usage` 流水；同一个码（`ref_type='redeem_code'`）只能产生一条 `redeem` 流水。重试、双发、worker 重复消费全部安全。

### 3.3 `redeem_code_batches`

| 列 | 说明 |
|---|---|
| `id` UUID PK | |
| `name` VARCHAR(128) | 批次名，如「2026 中秋活动」 |
| `credits_per_code` BIGINT, CHECK > 0 | 整批共享面额 |
| `expires_at` TIMESTAMPTZ NULL | NULL = 永久 |
| `note` TEXT NULL | |
| `created_by` UUID FK users.id ON DELETE SET NULL | |
| `created_at` / `updated_at` | |

### 3.4 `redeem_codes`

| 列 | 说明 |
|---|---|
| `id` UUID PK | |
| `batch_id` UUID FK ON DELETE CASCADE, index | |
| `code_hash` CHAR(64) UNIQUE NOT NULL | 规范化后明文的 SHA-256 hex |
| `code_prefix` VARCHAR(8) NOT NULL | 前 6 位，仅供管理员在列表中辨认 |
| `status` VARCHAR(16) NOT NULL | `active` / `redeemed` / `void` |
| `redeemed_by` UUID NULL FK users.id ON DELETE SET NULL | |
| `redeemed_at` TIMESTAMPTZ NULL | |
| `created_at` | |

索引：`(batch_id, status)`。

**码存哈希，不存明文。** 理由与代价：

- 兑换码是**不记名凭证**，等价于现金。明文入库意味着任何一次数据库泄露、备份外泄或日志误打即等于直接漏钱。
- 明文仅在**生成那一次响应**中返回，供管理员立即导出 CSV；此后系统内不再有明文，列表只显示前缀。
- 代价：CSV 丢失后这批码不可恢复，只能整批作废重发。这是有意接受的取舍。
- 安全性补偿：Crockford Base32、16 字符四组（`XXXX-XXXX-XXXX-XXXX`），80 bit 熵，配合兑换接口限流，枚举不可行。

规范化规则（哈希前）：去除所有非字母数字字符、大写、`I`/`L`→`1`、`O`→`0`。这让用户手输时的大小写与分隔符差异不影响兑换。

## 4. 三个不变量与并发

每个不变量都由**数据库级机制**兜底，不依赖应用层自觉。

### 4.1 一个码只能兑一次 — 单语句 CAS

```sql
UPDATE redeem_codes
   SET status = 'redeemed', redeemed_by = :uid, redeemed_at = now()
 WHERE id = :code_id AND status = 'active';
```

`rowcount == 1` 才算抢到。并发下只有一个事务能赢，输的一方拿到 0 行，走"已被兑换"分支。无需显式加锁。

过期与作废在 CAS 之前用同事务内的 `SELECT` 判定（批次通过 `code_hash` JOIN 出来，`expires_at` 与 `now()` 比较）。

### 4.2 一轮只扣一次 — 唯一部分索引

§3.2 的 `uq_credit_ledger_ref`。重复结算撞唯一约束抛 `IntegrityError`，捕获后按幂等命中处理（返回，不报错）。

### 4.3 余额不被并发写坏 — 账户行锁

每次变动前 `SELECT ... FROM credit_accounts WHERE user_id = :uid FOR UPDATE`。

`with_for_update()` 在 Postgres 上渲染为真行锁；SQLite 方言直接忽略该子句，而 SQLite 本身是单写者模型，所以测试库上语义依然正确 —— 一套代码，不用分叉。

**透支语义**：准入检查在轮前，扣费在轮后，所以最后一轮会扣成负数。不设 `CHECK (balance >= 0)`，因为：

1. 它会和透支打架（合法的最后一轮会被约束拒绝）；
2. 它把「平台确实花了这笔钱」这个事实藏起来。

改为：

- 准入条件是 `balance > 0`；
- 用户侧显示 `max(0, balance)`；
- 负余额是平台的真实负债，后台可见，可由对账 SQL 查出。

对账查询（余额 vs 流水求和）在 §9 的运维小节给出。

### 4.4 账户行必须存在

三重保证：迁移为所有存量用户回填；注册时创建；`credit_service._ensure_account()` 作为兜底（`SELECT ... FOR UPDATE` 未命中则 INSERT，撞唯一约束则回滚到 savepoint 重查）。

## 5. 扣分与拦截接入

### 5.1 统一结算入口（顺带修掉 §2.1 的漏洞）

新增异步入口，替换全部 5 处「记账 + 计费」调用：

```python
async def settle_turn_usage(
    db, user_id, message: Message, model_name: str | None, usage: dict | None
) -> None:
    """记账 + 积分扣减（同一 DB 事务）+ 配额计费（best-effort）。"""
```

原子性边界要说清楚：**记账与积分扣减在同一 DB 事务内**，要么都成立要么都不成立（唯一索引保证重复结算幂等）。**配额计费走 Redis，是 best-effort** —— 与 `quotas.py` 既有语义一致，Redis 不可用时降级为进程内计数，失败不影响积分账本。二者不构成一个事务，也不应该：积分是钱，配额是限流，可靠性要求不同。

- `ChatService._run`（`:1754`） — 替换
- `run_durable_turn` done 分支（`:2504`） — 替换
- `run_durable_turn` CancelledError 分支（`:2537`） — 替换
- `_finalize_error`（`:1911`） — 替换，**并新增 `user_id` 参数**（内联调用点 `:1807`、durable 调用点 `:2520` 一并传参）
- `_finalize_interrupted`（`:1928`） — 替换，同样新增 `user_id` 参数

改完之后，"消费了 token 却没计费" 在结构上不再可能。

三处调用点（`:1807` 内联 `_finalize_error`、`:1860` `_finalize_interrupted`、`:2520` durable `_finalize_error`）的 `user` 均在作用域内，加参数不需要额外查询。

### 5.2 扣分公式

```
cost_usd 已知且 > 0:     积分 = max(1, ceil(cost_usd * CREDITS_PER_USD))
cost_usd 未知但有 token: 积分 = max(1, ceil(total_tokens / 1000 * CREDITS_PER_1K_TOKENS_FALLBACK))
两者都无:                积分 = 0
```

第二行是必须的：`usage_cost()` 对未配置定价的模型返回 `None`（`app/core/pricing.py:75`），provider 也可能不报 `cost_usd`。没有兜底，这些模型就是免费额度。

整数积分，`ceil` 向上取整，真实消耗永远 ≥ 1 分。零用量（mock / 无 usage）不扣。

### 5.3 准入拦截

单一开关 `CREDITS_ENFORCED`（默认 `false`，观察模式）。关闭时：扣分照常记账、余额照常显示，但**不拦截**。这让上线可以分两步：先发码、核对扣分数字是否合理，再打开拦截。

两个准入点：

| 路径 | 位置 | 未通过时 |
|---|---|---|
| 内联流式 | `ChatService.stream()`，紧邻现有 `admit_run`（`:1076`） | SSE `error` 事件，`code="insufficient_credits"` |
| 持久化运行 | `create_and_enqueue_durable_run()`，入队**之前** | HTTP 402 |

都必须在调用模型**之前**拒绝，否则已经产生成本。

轮中不做二次拦截：一轮已经产生的成本必须结清，中途掐断只会让账更难对。下一轮准入自然拦住。

### 5.4 不扣分的消耗

平台自身开销不计入用户账：自动标题生成（`title_service`）、记忆自动提议、意图识别、RAG 向量化。理由：这些是平台为提供服务的固定成本，且用户无法控制其触发；把它们计入会让账单不可预测、无法解释。

这与 §5.1 的入口设计天然一致 —— 该入口只结算挂在用户轮次上的 `assistant` 消息。

## 6. 配置

`app/core/config.py` 新增：

| 键 | 默认 | 说明 |
|---|---|---|
| `CREDITS_ENFORCED` | `false` | 观察模式开关；`ENV=test` 下强制关闭 |
| `CREDITS_PER_USD` | `1000.0` | 1 美元成本 = 1000 积分 |
| `CREDITS_PER_1K_TOKENS_FALLBACK` | `1.0` | 未定价模型的每千 token 兜底扣分 |
| `CREDITS_SIGNUP_BONUS` | `0` | 注册赠送；0 = 不送 |
| `CREDITS_MAX_ADJUST` | `10000000` | 单次管理员调分绝对值上限（防误操作） |
| `REDEEM_MAX_CODES_PER_BATCH` | `5000` | 单批生成上限（防一次生成百万行） |

兑换接口的限流用字面量 `rate_limit_user(10, 60, "credits-redeem")`，不做成配置项 —— 代码库中所有限流（`auth` / `chat` / `artifacts` / `agent_runs` / `retrieval`）都是字面量，且限流在 `ENV=test` 下整体禁用，加一个配置项只会多一处不一致。

`CREDITS_ENFORCED` 在 `ENV == "test"` 时强制为 `false`，与 `quotas.py` / `rate_limit.py` 的既有约定一致 —— 测试套件默认不被拦截。需要测拦截的用例显式注入开启的配置。

`.env.example` 同步补齐。

## 7. API

### 7.1 用户侧 — `app/api/credits.py`，`router`，prefix `/api/credits`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/me` | `{balance, lifetime_granted, lifetime_consumed, enforced}` |
| POST | `/redeem` | body `{code}` → `{balance, credits_added, batch_name}` |
| GET | `/ledger?limit=&cursor=` | 倒序流水，游标分页 |

游标是 `"{created_at.isoformat()}_{id}"`，服务端按 `(created_at, id) < (cursor_created_at, cursor_id)` 取下一页。用 id 做次级排序键，避免同一时间戳的流水在分页时漏读或重读。

`/redeem` 挂 `rate_limit_user(REDEEM_RATE_LIMIT_PER_MIN, 60, "credits-redeem")`。

兑换错误码（区分原因，降低客服成本；80 bit 熵 + 限流已使枚举不可行）：

| 情况 | HTTP | code | 文案 |
|---|---|---|---|
| 码不存在 | 404 | `redeem_code_not_found` | 兑换码不存在，请检查是否输入有误 |
| 已被兑换 | 409 | `redeem_code_used` | 该兑换码已被使用 |
| 已过期 | 410 | `redeem_code_expired` | 该兑换码已过期 |
| 已作废 | 410 | `redeem_code_void` | 该兑换码已作废 |

### 7.2 管理侧 — `app/api/credits.py`，`admin_router`，prefix `/api/admin`

全部 `get_current_admin` 门禁。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/redeem-batches` | `{name, credits_per_code, count, expires_at?, note?}` → 批次 + **明文码数组**（唯一一次） |
| GET | `/redeem-batches` | 批次列表 + 进度 `{total, redeemed, void, active}` |
| GET | `/redeem-batches/{id}/codes` | 该批码列表（前缀、状态、兑换人、兑换时间） |
| POST | `/redeem-batches/{id}/void` | 作废该批所有 `active` 码 |
| GET | `/credits/accounts?search=&limit=&offset=` | 用户余额列表 |
| POST | `/credits/adjust` | `{user_id, delta, note}` → 新余额 |

手动调分：`delta` 非零且在 `±CREDITS_MAX_ADJUST` 内；写 `reason='admin_adjust'`、`ref_type='admin'`、`actor_id=管理员`的流水；同时写一条 `AuditEvent`（`action="credits:adjust"`），复用既有审计面板。

模块内两个 router 是既有约定（见 `app/api/memories.py` 的 `router` + `user_router`）。

## 8. 前端

### 8.1 新增页面 `/settings/credits`

- 余额卡片：大号余额、累计发放、累计消耗
- 兑换表单：输入框（自动大写、自动补分隔符）+ 提交；`409/410` 错误按 §7.1 文案 toast
- 流水表：时间、原因、变动、余额快照；游标"加载更多"
- `CREDITS_ENFORCED` 关闭时顶部显示一条"当前为观察模式，余额不足不会拦截"的提示 —— 避免用户看到扣分却没被拦而困惑

`app/settings/layout.tsx` 的 `NAV` 数组新增一项（`Coins` 图标）。

### 8.2 侧边栏常驻余额

`components/sidebar.tsx` 底部（设置入口附近）加一行余额，点击跳 `/settings/credits`。预付费模型下用户必须随时看得到还剩多少，否则被拦截时很突然。

### 8.3 管理后台

`app/admin/page.tsx` 新增两个 Tab：

- **兑换码**：批次列表（名称/面额/进度条/有效期）+ 新建批次对话框 + 生成后明文码弹窗（一键复制 / 下载 CSV）+ 整批作废
- **积分**：用户余额表（搜索）+ 调分按钮（对话框：增减 + 备注）

### 8.4 数据层

`lib/types.ts` 新增类型；`lib/api.ts` 新增方法；新增 `hooks/useCredits.ts` 暴露 `useCredits()`（React Query，key `["credits","me"]`），侧边栏与积分页共用同一份缓存。

余额变动后失效 `["credits","me"]` 与 `["credits","ledger"]`。

注：走 React Query，不引入新的 zustand store —— 避免已知的 selector 陷阱。

## 9. 迁移

新增 `backend/migrations/versions/0014_credits_and_redeem_codes.py`，`down_revision = "0013_wechat_identities"`。

内容：

1. 建四张表 + 全部索引（含 §3.2 的唯一部分索引）
2. **回填**：为每个存量用户插入 `credit_accounts` 行（balance 0）

回填不可省 —— 否则老用户没有账户行，只能依赖 `_ensure_account` 兜底，而"余额为 0"和"账户不存在"在排查时会变成两件不同的事。

顺带修掉 `docker-compose.prod.yml:101` 的过期注释（写着 head 是 `0010_artifacts`，实际已是 `0013`）。

运维对账 SQL（写进 `docs/`）：

```sql
SELECT a.user_id, a.balance, COALESCE(SUM(l.delta), 0) AS ledger_sum
  FROM credit_accounts a
  LEFT JOIN credit_ledger l ON l.user_id = a.user_id
 GROUP BY a.user_id, a.balance
HAVING a.balance <> COALESCE(SUM(l.delta), 0);
```

返回任何行即表示余额与流水漂移，需要人工介入。

## 10. 测试

### 10.1 后端 `backend/tests/test_credits.py`、`test_redeem_codes.py`

兑换：
- 兑换成功 → 余额增加、流水正确、码状态为 `redeemed`
- 重复兑换同一码 → `redeem_code_used`，余额不变
- 过期码 / 已作废码 / 不存在的码 → 各自错误码
- 码格式容错：小写、缺分隔符、`O`↔`0`、`I`↔`1` 均可兑换
- 明文只在创建响应中出现；列表接口只返回前缀（不泄露明文）

生成：
- 非管理员调用 → 403
- 批量生成 N 个码 → 哈希唯一、前缀正确
- 批次作废 → 该批 `active` 码全部变 `void`，已兑换的不受影响
- 超过 `REDEEM_MAX_CODES_PER_BATCH` → 400

扣分：
- 已知 `cost_usd` → 按 `CREDITS_PER_USD` 扣
- 未定价模型（`cost_usd` 为 `None`）但有 token → 走兜底扣分（**防止免费额度**）
- 零用量 → 不扣
- 同一 message 结算两次 → 只扣一次（幂等）
- 余额扣成负数 → 允许，流水 `balance_after` 正确

拦截：
- `CREDITS_ENFORCED=false` → 余额 0 仍可对话（观察模式）
- `CREDITS_ENFORCED=true` + 余额 0 → 内联路径收 `insufficient_credits` 事件；durable 路径收 HTTP 402
- `CREDITS_ENFORCED=true` + 余额 > 0 → 放行

**错误轮次计费**（覆盖 §2.1 的修复）：
- 一轮以 `error` 收尾但 provider 报了 usage → 必须扣分

调分：
- 管理员调分 → 余额变动 + 流水 `admin_adjust` + `AuditEvent`
- 超过 `CREDITS_MAX_ADJUST` → 400
- 非管理员 → 403

并发（测试套件跑在内存 SQLite 上，而 SQLite 是单写者模型，无法制造真并发）：

- **套件内**：确定性地摆出"陈旧预读"时序，验证 CAS 语句本身 —— 先用一个会话把码按 `active` 读出，另一个会话兑掉并提交，再对那个陈旧对象执行 CAS，必须得 0 行。这正是"先 SELECT 判断再 UPDATE"会踩的坑，且不依赖并发。
- **套件外**：真多写者行为在真实 Postgres 上手动演练（并发兑同码、并发发放、账本幂等），步骤写在 `docs/credits-operations.md` 的并发演练小节，**首次上线前做一次**。

不写"永远 skip 的 Postgres 测试" —— `tests/conftest.py` 强制 `DATABASE_URL` 为内存 SQLite，那种测试在 CI 里永远不会真跑，是假的生产级。

### 10.2 前端

`frontend/src/lib/__tests__/` 下覆盖：兑换码输入规范化、错误码到文案的映射、余额格式化（负余额显示为 0）。

## 11. 上线顺序

1. 部署迁移（建表 + 回填），此时功能对用户不可见
2. 保持 `CREDITS_ENFORCED=false`，发一批兑换码
3. 核对：余额增长是否符合预期、扣分数字是否合理、对账 SQL 无输出
4. 打开 `CREDITS_ENFORCED=true`

第 3 步是这次上线的关键安全点 —— 观察模式下拦截不生效，扣分记录可以先验证。

## 12. 影响面清单

后端新增：
- `app/credits.py`（纯函数：扣分公式、码生成与规范化）
- `app/models/credit_account.py`、`credit_ledger.py`、`redeem_code_batch.py`、`redeem_code.py`
- `app/services/credit_service.py`、`app/services/redeem_service.py`
- `app/schemas/credit.py`
- `app/api/credits.py`
- `migrations/versions/0014_credits_and_redeem_codes.py`
- `tests/test_credits.py`、`tests/test_redeem_codes.py`

后端修改：
- `app/models/__init__.py` — 注册新模型
- `app/core/config.py` — 新增配置项
- `app/main.py` — 挂载 router
- `app/services/chat_service.py` — §5.1 统一结算入口 + §5.3 两个准入点
- `app/api/auth.py` — 注册时创建账户行（+ 可选赠送）。注意注册逻辑在路由里，
  `app/services/auth_service.py` 的 `register()` 没有任何调用方，是死代码
- `app/schemas/__init__.py` — 导出新 schema
- `.env.example`
- `docker-compose.prod.yml` — 修过期注释

前端新增：
- `app/settings/credits/page.tsx`
- `hooks/useCredits.ts`
- `lib/__tests__/credits.test.ts`

前端修改：
- `app/settings/layout.tsx` — NAV 新增一项
- `app/admin/page.tsx` — 新增两个 Tab
- `components/sidebar.tsx` — 常驻余额
- `lib/api.ts`、`lib/types.ts`

文档：
- `docs/credits-operations.md` — 对账 SQL、发码流程、观察模式切换
