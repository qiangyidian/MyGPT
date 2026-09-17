# 微信公众号扫码登录（跨 MyGPT / sql2er 共用同一公众号）

日期：2026-09-17
状态：已批准，实施中

## 背景与目标

把 `D:\Gitee\sql2er` 已有的「微信公众号验证码登录」移植到 MyGPT，并让**两个服务共用同一个公众号**：
任一服务的用户扫码后拿到的 6 位验证码，在**两个站点都能登录**。

sql2er 的方案（见其 `docs/wechat-login.md`）是：扫码 → 关注/发关键词 → 公众号被动回复 6 位码 →
网页输入码 → 换取会话。不依赖网页授权（OAuth），因此任何能在后台看到「服务器配置」的公众号都能用。

## 约束（决定了整个架构）

1. **微信后台「服务器配置」只能填一个 URL + 一个 Token。** 一条消息只推给一个地址，无法按消息分流。
2. **微信的被动回复只显示一个。** 因此用户微信里看到的码只有一个，它必须在两个站点都有效。
3. 两个服务在两台不同服务器上，各自有独立的 Redis。

由 1+2 推出：两个服务各自独立生成随机码的做法**必然失效**（用户看到 A 的码，在 B 输入校验不过）。
要共用，码必须两边一致 —— 必须引入共享层。

## 已否决的方案

| 方案 | 否决原因 |
|---|---|
| 各自独立随机码 + nginx mirror | 用户只看到主服务的码，在另一站登不了 |
| MyChat 中继采纳 sql2er 的码 | 要从 sql2er 的中文回复文本里正则抠数字，改文案即断；且 MyChat 发码寄生在 sql2er 上 |
| 共享 Redis（只有 sql2er 发码，MyChat 读同一 key） | 把两个生产服务的故障域绑在一起，且 MyChat 登录完全依赖 sql2er 在线 |
| MyChat 单独注册一个公众号 | 用户明确要求共用同一个 |

## 采用的方案：确定性派生码

两边从**同一条微信消息**派生出**同一个**码，各自独立存储、独立校验。

```
code = HMAC_SHA256(wechat_token, f"{openid}:{create_time}") 取模 10^6，补零成 6 位
```

密钥**不新增**，直接用公众号 Token（两边本来就同值）。

**决定性设计：`create_time` 取自微信消息体里的 `CreateTime`，不取服务器时间，也不做时间分桶。**
nginx mirror 复制的是同一条 XML，两边读到的 `CreateTime` 逐字节相同 → 派生结果必然相同，
**彻底消除两服时钟偏差这个失败源**。

不分桶是刻意的（实施期修正了初版设计）：分桶会让同一个时间窗内的每次请求都派生出**同一个码**，
于是用户重新发关键词要码时，一个**已被消费的码会被复活**——而本功能的典型用法恰恰是
「在 MyChat 登录后，再去 sql2er 发一次关键词拿码」。按消息派生之后：

- 微信 5 秒重推的是**同一条消息**（`CreateTime` 不变）→ 派生同一个码，保留「重推复用」语义；
- 用户主动再发是**新消息**（`CreateTime` 不同）→ 派生新码，已消费的码不会复活。

### 数据流

```
用户扫码 ──► 公众号（关注事件 / 发「验证码」）
             │
             ▼
   微信后台服务器配置 URL = https://sql2er.qiangi.top/api/v1/wechat/callback   ← 保持不变
             │
             ▼  sql2er 宿主机 nginx
             ├── proxy_pass ──► sql2er API   → 派生码 → 写自己的 Redis → XML 回复给微信（用户看到这个码）
             └── mirror      ──► https://mychat.qiangi.top/api/wechat/callback（响应丢弃）
                                       └─► MyChat 用同一算法派生同一个码 → 写自己的 Redis
             │
             ▼
用户输入该码 ──► MyChat POST /api/auth/login/wechat   → 查 openid → 登录 / 自动建号
                  sql2er POST /api/v1/auth/login/wechat → 同样能登进 sql2er
```

两边运行时零耦合：sql2er 停机不影响 MyChat 扫码登录（mirror 是异步的，不占微信那 5 秒超时）。

## 安全不变量

### 1. 派生码冲突必须失败关闭（硬性）

派生码失去随机性，存在碰撞：两个 openid 可能从各自的消息派生出同一个码。若后到的 openid
**覆盖**了 `code → openid` 映射，先到的用户输入自己的码就会**登进后到者的账号** —— 账号接管级漏洞。

因此写入一律是**原子且不覆盖**的（MyChat 用 Redis `SET NX`；sql2er 为此给
`CodeStore` 新增 `set_if_absent`——用 get-then-set 的话，并发下后到的 openid 仍会覆盖先到的映射，
正好复现要防的账号接管）。撞码时**不发这个码、也不动已有映射**，两边都回
「验证码服务繁忙，请稍后重试」的 200 文案（不是 503：微信重推的是同一条消息，派生同一个码、
撞同一个占用，重试永远解决不了问题）。

后果被限制为「这两位用户本轮拿不到码，让用户稍后再发一次」，**不会串号**。

### 2. 其余照抄 sql2er 的既有不变量

- 回调 GET / POST **均**强制 SHA-1 验签 —— Token 是回调方唯一凭证。
- 握手必须明文 `text/plain` 返回 `echostr`，消息回复必须明文 `text/xml`（FastAPI 默认 JSON 序列化会带引号，微信后台必然提交失败）。
- 验证码一次性（Redis `GETDEL` 原子消费）、绑定 openid、TTL 5 分钟。
- **限流判定必须在消费之前** —— 否则猜中的那一次会直接穿过闸门，且被拦的请求不该消耗掉他人待用的码。
- 回调 body 上限 64 KB，边读边断流（不要让公网端点在解析前把任意大小 body 读进内存）。

## MyGPT 改动清单

### 后端

| 文件 | 内容 |
|---|---|
| `app/core/wechat_mp.py` | 新增。`sha1_signature` / `parse_wechat_xml` / `build_text_reply` / `derive_login_code`（MyGPT 无 `app/utils` 包，放进已有的 `app/core/`） |
| `app/api/wechat.py` | 新增路由：`GET/POST /api/wechat/callback`、`GET /api/wechat/login-info` |
| `app/services/wechat_mp_service.py` | 新增。发码（派生 + `SET NX` 失败关闭）、登录换会话、绑定/解绑 |
| `app/models/wechat_identity.py` | 新增 `WechatIdentity`（`openid` unique、`user_id` FK、`bound_at`），登记进 `models/__init__.py` |
| `migrations/versions/0013_wechat_identities.py` | 新表；`down_revision = "0012_token_version_and_msg_index"` |
| `app/api/auth.py` | 加 `POST /api/auth/login/wechat`、`GET/POST/DELETE /api/auth/wechat/binding` |
| `app/schemas/auth.py` | 加 `WechatCodeLoginRequest` / 绑定相关；**`UserOut.email`: `EmailStr` → `str`**（见下） |
| `app/core/config.py` | 加 `WECHAT_MP_*` 配置 + 生产守卫（开启时要求 Token 非默认值且 32 位） |

`UserOut.email` 放宽为 `str` 的理由：它是**输出** schema，出站再校验邮箱格式不带来价值，却会被任何合成地址炸掉。
实测 pydantic 2.12 + email-validator 2.3 拒绝 `.invalid` / `.local` 这类特殊用途域名，而
`auth.py` 的注销流程正好写 `deleted-xxx@deleted.invalid` —— `GET /api/admin/users`
（`admin.py:58`，对全部用户 `UserOut.model_validate`）只要有一个用户注销过就 **500**。
这是本次顺带修掉的既有 bug。进站 `RegisterRequest.email` 仍是 `EmailStr`，校验未削弱。

### 自动建号

`users` 的 `email` / `username` / `password_hash` 均 NOT NULL + UNIQUE，故合成：

- `email` → `wx_{openid}@wechat.local`
- `username` → `微信用户{openid 后 6 位}`，撞名则加 4 位随机后缀
- `password_hash` → `hash_password(secrets.token_urlsafe(32))`（无人知道的随机值，用户可另走改密流程）
- `role="user"`，`is_active=True`

### 前端

| 文件 | 内容 |
|---|---|
| `public/images/wechat-account-qrcode.jpg` | 从 sql2er 复制 |
| `app/login/page.tsx` | 加第三个 tab「公众号验证码」：二维码 + 三步引导 + 6 位码输入框；`login-info` 未配置时降级为纯文字提示 |
| `app/settings/account/page.tsx` + settings `layout.tsx` 的 NAV | 新增「账号安全」页：绑定微信 / 解绑 / 绑定状态 |
| `lib/api.ts` | `loginWithWechatCode` / `fetchWechatLoginInfo` / `fetchWechatBinding` / `bindWechat` / `unbindWechat` |

绑定入口不可省：否则既有用户（如 admin）一扫码就凭空多出一个空账号，登不回自己的号。

## sql2er 改动清单

- `app/utils/wechat.py`：加 `derive_login_code(openid, create_time, token)`。
- `app/utils/code_store.py` + `app/utils/redis_code_store.py`：给 `CodeStore` 新增原子的
  `set_if_absent`（内存后端惰性清理过期项；Redis 后端用 `SET NX`）。这是「失败关闭」的载体。
- `app/api/wechat.py` 的 `_issue_login_code`：把 `_generate_unused_code()` 的随机值换成派生值，
  **去掉 `_pending_code` / 反向索引**（按消息派生后，重推必然得到同一个码，复用索引不再需要），
  写入改用 `set_if_absent` 并在被他人占用时回「繁忙」文案。
- 其自身扫码登录行为不变（仍是 6 位、5 分钟、一次性），但既有测试需按新语义更新。

## 服务器侧（非代码）

- sql2er 宿主机 nginx 增加 mirror snippet。注意仓库里 `infra/docker/nginx-*.conf` 是 **docker 版**，
  线上是**宿主机 nginx**，两者不是同一个文件。
- sql2er 与 MyChat 的 `WECHAT_MP_TOKEN` / `SQL2ER_WECHAT_TOKEN` 必须**逐字符一致**。
  真 Token 不在任何仓库里，只存在于 sql2er 服务器 `/etc/sql2er/env` 与公众号后台「服务器配置」页。
- 公众号后台：URL 不变，**消息加解密方式必须为明文模式**（后端只实现明文验签与明文 XML 解析）。
- 部署顺序：先在服务器配好 Token（sql2er 生产启动会校验，不满足则拒绝启动 → 全站 502），再推代码。

## 测试

**MyGPT 后端** `tests/test_wechat_mp.py`

- 握手：验签通过返回明文 `echostr`；验签失败 403
- 消息：关注事件发码 / 关键词发码 / 无关键词不回复
- 响应必须是 `text/xml` 明文，不带 JSON 引号
- 码 TTL 5 分钟；过期被拒；**用过的码不能重放**
- **两个后端对同一条微信消息派生出同一个码**（本次架构的命门，必须钉死）
- **派生碰撞时不覆盖已有映射**（失败关闭，不串号）
- 自动建号：新 openid 建号并绑定；重复扫码登回同一账号
- 绑定：已登录用户绑定；openid 已绑他人 → 409
- 限流判定先于消费
- `UserOut.email` 接受合成地址（回归钉死管理员用户列表的 500）

**MyGPT 前端** vitest：登录页 tab 渲染、未配置时降级、验证码提交

**sql2er**：跑既有 `test_wechat_login_flow.py` / `test_wechat_login_rate_limit.py` / `test_wechat_config.py`，按新语义更新

## 环境变量（MyGPT）

```bash
# 总开关
WECHAT_MP_ENABLED=true
# 必须与公众号后台「服务器配置」的 Token 完全一致（两边共用同一串，32 位）
WECHAT_MP_TOKEN=
# 登录页展示的公众号二维码；留空则不显示扫码引导，仅文字提示
WECHAT_MP_LOGIN_QR_URL=/images/wechat-account-qrcode.jpg
# 发码关键词，默认「验证码」（回复「登录」永远可用）
WECHAT_MP_KEYWORD=验证码
# 码的有效期（秒）
WECHAT_MP_CODE_TTL_SECONDS=300
# 失败计数（IP 维度）
WECHAT_MP_LOGIN_MAX_ATTEMPTS=10
WECHAT_MP_LOGIN_LOCKOUT_SECONDS=900
```
