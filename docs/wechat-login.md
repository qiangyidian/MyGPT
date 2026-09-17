# 微信公众号扫码登录 —— 配置与上线指南

> 用户流程：登录页切到「公众号验证码」tab → 微信扫码关注公众号 → 公众号自动回复 6 位验证码 →
> 网页输入验证码 → 登录成功（新用户自动建号并绑定 openid）。
>
> **MyChat 与 sql2er 共用同一个公众号**，用户扫一次码拿到的 6 位码，在**两个站点都能登录**。

---

## 一、为什么需要额外做一层「共用一个公众号」

微信后台「设置与开发 → 服务器配置」**只能填一个 URL、一个 Token**，一条消息只推给一个地址；
而且微信的被动回复**只会显示一条**。

所以两个服务各发各的随机码是行不通的：用户微信里看到的只会是其中一个服务生成的码，
拿去另一个服务输入必然校验失败。要让两边都能用同一个码，这个码必须在两边**一致**。

本实现的办法是**确定性派生**：两个后端从同一条微信消息算出同一个码，彼此不共享存储、也没有运行时依赖。

```
用户扫码 ──► 公众号（关注事件 / 发「验证码」）
             │
             ▼
   微信后台服务器配置 URL = https://sql2er.qiangi.top/api/v1/wechat/callback   ← 保持不变
             │
             ▼  sql2er 宿主机 nginx
             ├── proxy_pass ──► sql2er API  → 派生码 → 写自己的 Redis → XML 回复给微信（用户看到这个码）
             └── mirror      ──► https://mychat.qiangi.top/api/wechat/callback（响应丢弃）
                                       └─► MyChat 用同一算法派生同一个码 → 写自己的 Redis
             │
             ▼
用户输入该码 ──► MyChat  POST /api/auth/login/wechat     → 查 openid → 登录 / 自动建号
                  sql2er POST /api/v1/auth/login/wechat  → 同样能登进 sql2er
```

派生算法（两边逐字节一致）：

```
code = HMAC_SHA256(wechat_token, f"{openid}:{create_time}") 取模 10^6，补零成 6 位
```

`create_time` 取自**消息体里的 `CreateTime`**，不取服务器时间：mirror 复制的是同一份 XML，
两边读到的字节完全相同，所以**两台服务器的时钟偏差不会让它们算出不同的码**。
也正因为按消息（而不是按时间窗）派生，用户重新发一次关键词会得到**新码**，
而微信 5 秒重推同一条消息会得到**同一个码**（保留「重推复用」语义）。

**两边完全独立**：sql2er 停机不影响 MyChat 扫码登录，反之亦然（mirror 是异步的，不占微信那 5 秒超时）。

### 公众号回复的文案为什么不写应用名

微信里看到的是「欢迎关注！您的验证码是：XXXXXX」——**刻意不出现 MyChat 或 SQL2ER**。

原因是架构决定的，不是措辞偏好：微信服务器配置一个号只有一个 URL、被动回复也只显示一条
（展示给用户的是主回调 sql2er 那一条，MyChat 的回复被 mirror 丢弃），而消息体里只有
`openid` / `CreateTime` / 正文，**没有任何"用户想登哪个站"的信息**。所以文案做不到按应用区分。

而这条码在两边都能登录，点名其中任何一个反而会误导从另一个站点扫码的用户。
真要做到按应用区分，得给每个站点配**带参数二维码**（`qrcode/create` API，靠 `Scene` 区分），
那需要 AppID/AppSecret，也要放弃"一个二维码两边通用"——本流程刻意避开了这条依赖。

两侧仓库都有测试钉住这一点（`test_reply_text_names_no_application` /
`test_reply_text_does_not_name_either_application`），别把应用名加回去。

---

## 二、你需要准备的东西

| # | 材料 | 从哪拿 | 用在哪 |
|---|------|--------|--------|
| 1 | **Token** | **直接复用 sql2er 已经在用的那个**：登录公众号后台「服务器配置」可见明文，或从 sql2er 服务器 `/etc/sql2er/env` 里的 `SQL2ER_WECHAT_TOKEN` 取 | 两边 `.env` 里填**同一个值**，必须 32 位 |
| 2 | **公众号二维码图片** | 公众号后台「设置与开发 → 公众号设置 → 账号二维码」下载 | 已经放好：`frontend/public/images/wechat-account-qrcode.jpg`（随前端一起发布，同域无防盗链问题） |
| 3 | **回调 URL** | — | **不用改**，还是 `https://sql2er.qiangi.top/api/v1/wechat/callback` |

**不需要 AppID / AppSecret**：本流程只依赖消息推送，不碰网页授权（OAuth），因此也拉不到昵称头像，
自动建号的昵称是「微信用户+openid 尾号」。

### 公众号后台侧的唯一要求

「服务器配置 → 消息加解密方式」必须是 **明文模式**。后端只实现了明文验签与明文 XML 解析；
选安全模式或兼容模式时 body 是密文，解析会失败。

---

## 三、部署步骤（按顺序）

### 1. MyChat：配好 Token 再重启后端

```bash
ssh root@<MyChat 服务器>
cd /root/MyGPT
# WECHAT_MP_TOKEN 填与 sql2er 完全一致的那 32 位
sed -i '/^WECHAT_MP_/d' .env
cat >> .env <<'EOF'
WECHAT_MP_ENABLED=true
WECHAT_MP_TOKEN=<与 SQL2ER_WECHAT_TOKEN 逐字符相同>
WECHAT_MP_LOGIN_QR_URL=/images/wechat-account-qrcode.jpg
WECHAT_MP_KEYWORD=验证码
WECHAT_MP_CODE_TTL_SECONDS=300
WECHAT_MP_LOGIN_MAX_ATTEMPTS=10
WECHAT_MP_LOGIN_LOCKOUT_SECONDS=900
EOF

# 生产守卫：ENV 非 dev/test 时，WECHAT_MP_ENABLED=true 而 Token 为空/默认值/非 32 位会拒绝启动
systemctl restart mychat-backend
journalctl -u mychat-backend -n 30 --no-pager   # 确认起来了
curl -s http://127.0.0.1:8003/api/wechat/login-info
```

期望输出：`{"data":{"configured":true,"qrcode_url":"/images/wechat-account-qrcode.jpg","keyword":"验证码"}}`

### 2. MyChat：跑数据库迁移（建 `wechat_identities` 表）

```bash
cd /root/MyGPT/backend && source /opt/mychat-venv/bin/activate
alembic upgrade head
```

> 迁移是幂等的：如果这张表已经由 `create_all` 建出来了，它会直接跳过。

### 3. MyChat：重新构建前端（二维码图必须进构建产物）

```bash
cd /root/MyGPT/frontend
NEXT_PUBLIC_API_BASE_URL=https://mychat.qiangi.top npm run build
systemctl restart mychat-frontend
ls /root/MyGPT/frontend/public/images/   # 应有 wechat-account-qrcode.jpg
```

### 4. sql2er：带上本次改动

sql2er 的改动是「发码改为派生 + `CodeStore.set_if_absent`」。**推 master 即自动上线**，
所以顺序是：**先在 MyChat 侧全部就绪（上面 1–3 步），再推 sql2er**。
sql2er 的 `.env` **不用改**（Token 本来就是同一个）。

### 5. sql2er 宿主机 nginx：加 mirror

> ⚠️ 仓库里的 `infra/docker/nginx-*.conf` 是 **Docker 版**；线上跑的是**宿主机 nginx**，
> 两者不是同一个文件。改的是宿主机上 `sql2er.qiangi.top` 那个 server 块。

找到现在把 `/api/v1/wechat/callback` 转给 sql2er API 的 location（形如 `location /api/ {...}`），
在它里面加一行 `mirror`，并新增一个 `internal` 的 mirror location：

```nginx
# 把同一条微信消息复制一份给 MyChat。子请求的响应会被丢弃——
# 微信永远只看到上面 proxy_pass 的回复。
mirror /__mirror_wechat;
mirror_request_body on;

location = /__mirror_wechat {
    internal;                      # 只允许内部子请求，公网访问不到
    # $is_args$args 必须带上：验签用的 signature/timestamp/nonce 都在 query 里，
    # 丢了这几个参数 MyChat 侧验签必然失败（403）。
    proxy_pass https://mychat.qiangi.top/api/wechat/callback$is_args$args;
    proxy_set_header Host mychat.qiangi.top;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_ssl_server_name on;
}
```

```bash
nginx -t && systemctl reload nginx
```

**MyChat 挂了会怎样？** mirror 是异步子请求，失败只会在 nginx 的 error log 里留一条，
**不影响 sql2er 的回复**，用户在 MyChat 那侧登不了、在 sql2er 侧照常登。

### 6. 验证（先别急着让用户用）

```bash
# a) 握手自检：期望是【纯文本】hello，不是带引号的 "hello"
TS=1700000000; NONCE=test123
SIG=$(python3 -c "import hashlib;print(hashlib.sha1(''.join(sorted(['<TOKEN>','$TS','$NONCE'])).encode()).hexdigest())")
curl "https://sql2er.qiangi.top/api/v1/wechat/callback?signature=$SIG&timestamp=$TS&nonce=$NONCE&echostr=hello"
```

```bash
# b) 端到端：真机扫码，拿到 6 位码后【分别在两个站点输入】，都应能登录
```

---

## 四、接口清单

| 端点 | 说明 |
|---|---|
| `GET /api/wechat/callback` | 微信握手 echostr 校验（**明文**返回） |
| `POST /api/wechat/callback` | 消息推送：`subscribe` 事件 / 关键词文本 → 回复验证码（明文 XML） |
| `GET /api/wechat/login-info` | 公开：登录页扫码引导（`configured` / `qrcode_url` / `keyword`） |
| `POST /api/auth/login/wechat` | 网页端提交 `{wechat_code}` → 换取会话（自动注册） |
| `GET /api/auth/wechat/binding` | 当前账号的绑定状态 |
| `POST /api/auth/wechat/binding` | 把扫码的微信**绑定到当前登录账号** |
| `DELETE /api/auth/wechat/binding` | 解绑 |

**一定要用绑定入口。** 否则既有账号（比如 admin）一扫公众号就会**凭空多出一个空账号**，
而不是登回自己的号。入口在「设置 → 账号安全」。

---

## 五、安全要点（不要改动）

- 回调 GET / POST **都**强制 SHA-1 验签 —— Token 是回调方唯一凭证。Token 泄露等于
  任何人可伪造一条消息推送、为任意 openid 签发登录码。所以生产守卫会让弱/默认 Token **起不来**，
  而不是静默运行。
- **派生码撞码时失败关闭**：写入是**原子且不覆盖**的（`SET NX` / `set_if_absent`）。
  如果这里用 get-then-set，并发下后到的 openid 会覆盖先到的映射，先到的用户输入自己的码
  就会**登进后到的账号**。撞码时两边都回「验证码服务繁忙」而不是发码。
- 握手必须明文 `text/plain`，消息回复必须明文 `text/xml`。FastAPI 默认会把字符串序列化成带引号的
  JSON，微信后台**必然提交失败**。
- 验证码一次性（Redis `GETDEL` 原子消费）、TTL 5 分钟、绑定 openid。
- **限流判定必须在消费之前**，否则猜中的那一次会直接穿过闸门，而且被拦的请求会消耗掉别人待用的码。
- 「一个公众号只能填一个回调 URL」是微信的硬约束 —— 不要试图在后台填两个地址。

---

## 六、本地 / 联调

自动化测试（不需要真实公众号，直接驱动等价链路）：

```bash
# MyChat
cd backend
python -m pytest tests/test_wechat_mp.py tests/test_wechat_mp_api.py -q
cd ../frontend && npx vitest run src/components/__tests__/wechat-login-panel.test.tsx

# sql2er
cd D:/Gitee/sql2er/apps/api
python -m pytest tests/test_wechat_login_flow.py tests/test_wechat_login_rate_limit.py \
                 tests/test_code_store_sweep_and_discard.py tests/test_redis_code_store.py -q
```

其中 `test_derive_login_code_matches_cross_repo_contract`（MyGPT）与
`test_derive_login_code_matches_cross_backend_contract`（sql2er）用**同一张 golden vector 表**。
任一边的实现漂移都会让自己这边的测试变红 —— 这是「两边必须算出同一个码」这条命门的守卫，
**不要为了让它变绿去改向量**。

真机联调：微信要求回调地址是公网 80/443 地址，本地必须先把服务暴露出去
（公众号测试号，或 natapp/ngrok 之类内网穿透临时填进后台）。

---

## 七、回滚

1. **只想关掉 MyChat 侧**：`.env` 里 `WECHAT_MP_ENABLED=false` → `systemctl restart mychat-backend`。
   回调返回 503、登录接口返回 503，登录页自动降级成纯文字提示。sql2er 不受影响。
2. **想彻底停用**：公众号后台「服务器配置」取消启用/改回原状，并移除 nginx 的 mirror 行。
3. sql2er 的回滚：`git revert` 本次提交并推 master。注意它自己的扫码登录在派生实现下仍然正常
   （6 位、5 分钟、一次性，行为对用户不可见）。
