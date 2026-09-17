# 积分与兑换码 — 运维手册

## 上线顺序

**不要跳过第 3 步。** `CREDITS_ENFORCED` 默认 `false`（观察模式），这是有意
设计的：直接开拦截会把所有余额为 0 的老用户在部署瞬间锁在外面。

1. **部署迁移**（建表 + 为存量用户回填账户行）。此时功能对用户不可见。
   ```bash
   # compose 部署
   docker compose -f docker-compose.prod.yml run --rm migrate
   # 或裸机部署
   bash deploy/mychat-deploy.sh
   ```
2. **保持 `CREDITS_ENFORCED=false`**，在后台「兑换码」Tab 生成一批码并发放。
3. **核对三项**：
   - 用户兑换后余额增量符合预期
   - 真实对话的扣分数字合理（在「设置 → 积分」的流水里看「对话消耗」）
   - **对账 SQL 无输出**（见下）
4. **打开拦截**：把 `CREDITS_ENFORCED=true` 写入 `.env` 并重启后端。

### 一个只影响"升级"而不影响"首次上线"的点

兑换码在库里是**哈希存储**的。本次上线用的哈希算法是 **HMAC-SHA256 + 服务端 pepper**
（`REDEEM_CODE_PEPPER`，留空时从 `JWT_SECRET` 派生；见 `app/credits.py` 的 `hash_code`）。

**若这是本功能第一次上线，你不需要做任何事**——库里还没有任何兑换码。

**但如果你曾经部署过本功能的中间版本并已经发出过兑换码**，那批码的哈希是用**旧的裸
SHA-256** 写的，本次升级后它们**将无法再兑换**（哈希不再匹配）。处理方式：把那些旧批次
作废，用新版本重新生成并重新发放。这也是为什么**首发前不要跳过上面的第 3 步核对**——
一旦码发出去，明文就取不回来了。

## 对账 SQL

余额是派生值（真相是账本流水求和），两者可能因程序缺陷漂移。这条查询
返回**任何行**都表示需要人工介入：

```sql
SELECT a.user_id, a.balance, COALESCE(SUM(l.delta), 0) AS ledger_sum
  FROM credit_accounts a
  LEFT JOIN credit_ledger l ON l.user_id = a.user_id
 GROUP BY a.user_id, a.balance
HAVING a.balance <> COALESCE(SUM(l.delta), 0);
```

修复方式：不要直接改 `credit_accounts.balance`（那会绕过账本，下次对账仍然
报警）。应该用后台的「调整积分」插一条 `admin_adjust` 流水把差额补上，并在
备注里写明原因。

建议把这条查询挂进日常巡检（例如随备份脚本一起跑）。

## 查某个用户的流水（计费争议排查）

用户投诉"一条消息扣了我 XXX 分"时，先看这个用户的流水行（reason / ref /
note / actor），优先用后台接口：
`GET /api/admin/credits/ledger?user_id=<uuid>&limit=50`（管理员鉴权，支持
`cursor` 分页，与用户侧 `/api/credits/ledger` 行形状一致）。应急时也可以直接：

```sql
SELECT created_at, delta, balance_after, reason, ref_type, ref_id, note
  FROM credit_ledger
 WHERE user_id = '<uuid>'
 ORDER BY created_at DESC
 LIMIT 50;
```

流程 Expectation：找到对应那一轮的 `turn_charge` 行，`ref_id` 指向 run /
message，可对照 `runs` 表核对当轮的 usage 与成本。

## 并发演练（首次上线前做一次）

测试套件跑在内存 SQLite 上，而 SQLite 是单写者模型 —— **它无法验证真并发**。
积分涉及钱，所以真多写者的行为必须在真实 Postgres 上手动验一次。

### 演练一：同一个码被并发兑换

开两个 psql 会话，都先读后写，验证只有一个能拿到行：

```sql
-- 会话 A 与 B 同时执行；把 :code_hash 换成某个 active 码的哈希
BEGIN;
UPDATE redeem_codes
   SET status = 'redeemed', redeemed_by = '<uuid>', redeemed_at = now()
 WHERE code_hash = :code_hash AND status = 'active';
-- 期望：其中一个会话 rowcount = 1，另一个 rowcount = 0
```

预期：**恰好一个 1，一个 0**。若两个都是 1，说明 CAS 的 WHERE 没生效，立刻停下排查。

### 演练二：并发发放不丢钱

```sql
-- 两个会话同时对同一用户插一条发放流水并更新余额
BEGIN;
SELECT balance FROM credit_accounts WHERE user_id = :uid FOR UPDATE;   -- 应阻塞其中一个
UPDATE credit_accounts SET balance = balance + 100 WHERE user_id = :uid;
COMMIT;
```

预期：`SELECT ... FOR UPDATE` 会阻塞第二个会话直到第一个提交；两个都提交后余额
恰好 +200。若第二个会话没被阻塞，说明行锁没生效。

### 演练三：账本幂等

```sql
-- 故意插一条重复的扣费流水
INSERT INTO credit_ledger (id, user_id, delta, balance_after, reason, ref_type, ref_id, created_at)
VALUES (gen_random_uuid(), :uid, -10, 0, 'usage', 'message', :msg_id, now());
-- 再插一次完全相同的 (ref_type, ref_id, reason)
```

预期：第二次抛 `duplicate key value violates unique constraint "uq_credit_ledger_ref"`。
若第二次成功，说明唯一部分索引没建上，检查迁移 0014 是否完整执行。

## 常见问题

### 用户说兑换码用不了

先问清楚错误提示：

| 提示 | 含义 | 处理 |
|---|---|---|
| 兑换码不存在 | 输错了，或这个码不属于本平台 | 让用户核对，注意 `0`/`O`、`1`/`I` 已自动纠正 |
| 该兑换码已被使用 | 已经被兑过 | 后台「兑换码」Tab 查该批次的码列表，用前缀定位是不是同一张 |
| 该兑换码已过期 | 超过批次有效期 | 无法恢复，需要重新发一张 |
| 该兑换码已作废 | 批次被整批作废 | 同上 |

### 兑换码明文丢了

**无法找回。** 库里只存 peppered HMAC-SHA256 哈希，这是有意设计（兑换码是不记名凭证，
等价于现金；明文入库意味着一次库泄露就等于漏钱）。处理方式：把该批作废，
重新生成一批。已兑换出去的分数不受影响。

### 用户余额是负数

正常现象，不是 bug。准入检查在对话开始前、扣费在对话结束后，所以最后一轮
会把余额扣成负数 —— 平台确实为那一轮付了钱。用户侧显示为 0，后台显示真实
负值。该用户下次兑换 / 获得积分后会自然补正。
### 想给某个用户补分

后台「积分」Tab → 搜索该用户 → 「调整」→ 填正数 + 备注。会写一条
`credits:adjust` 审计事件，「审计日志」Tab 可查。

## 配置项

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `CREDITS_ENFORCED` | `false` | 观察模式开关。`false` = 扣分记账但不拦截 |
| `CREDITS_PER_USD` | `1000` | 1 美元服务端实测成本折算多少积分 |
| `CREDITS_PER_1K_TOKENS_FALLBACK` | `1` | 未配置定价的模型每千 token 兜底扣分 |
| `CREDITS_SIGNUP_BONUS` | `0` | 注册赠送积分，0 = 不送 |
| `CREDITS_MAX_ADJUST` | `10000000` | 单次管理员调分上限 |
| `REDEEM_MAX_CODES_PER_BATCH` | `5000` | 单批生成上限 |
| `REDEEM_CODE_PEPPER` | 空 | 兑换码哈希的 HMAC pepper；留空回落到由 `JWT_SECRET` 派生，任何部署都不会退化成无密钥哈希 |

### 定价与积分的关系

扣分优先按 `MODEL_PRICING_JSON` 算出的服务端实测成本换算：

```
积分 = ceil(cost_usd × CREDITS_PER_USD)
```

**未在该表里配置的模型**（`usage_cost()` 返回 `None`）会回落到 token 计价：

```
积分 = ceil(total_tokens / 1000 × CREDITS_PER_1K_TOKENS_FALLBACK)
```

所以新增模型时记得同时配置 `MODEL_PRICING_JSON`，否则它会按兜底费率计费 ——
兜底费率与真实成本可能差很多，贵模型会被低估。

## 与配额（QUOTAS）的关系

两者独立叠加，互不影响：

- **配额**（`QUOTAS_ENABLED`）是管理员配的**上限**，不随充值增长，用户看不到。
- **积分**（`CREDITS_ENFORCED`）是用户可兑换的**余额**，有流水，用户可见。

任意一个开着都会拦截超限的请求，错误码不同（`quota_exceeded` vs
`insufficient_credits`）。
