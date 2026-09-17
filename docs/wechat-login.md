# 微信公众号扫码登录

> 用户流程：登录页「公众号验证码」tab → 扫码关注（或发关键词）→ 公众号回复 6 位验证码 →
> 网页输入 → 登录成功（新用户自动建号并绑定 openid）。

**MyChat 不直接对接微信。** 回调、回复文案、发码全部在一个独立服务
[`wechat-auth`](https://github.com/qiangyidian/wechat-auth) 里。MyChat 只做一件事：
把用户输入的码交给它，换回 openid。

## 为什么拆出去

微信「服务器配置」**一个公众号只能填一个回调 URL**，而被动回复**只显示一条**。
多个产品共用同一个公众号时，如果各自发码，就必须保证**算出同一个码**（否则用户在一侧
看到码、在另一侧登不上）—— 于是派生算法、跨仓库一致性测试、撞码失败关闭、nginx mirror、
两边 Token 逐字节一致全来了，每接一个新应用都要复制一遍。

把扫码收成一个服务之后：**它成为唯一的回调方与唯一发码方**，各应用只做
`openid → 自己的用户` 映射。接新应用 = 在 `/etc/wxauth/apps.json` 加一条 + 调一个接口。

```
                         ┌───────────────────────────────┐
   微信 ──回调──►        │  wechat-auth (wxauth.qiangi.top)│
                         │  回调 / 发码 / 回复文案 / 二维码  │
                         └───────────────────────────────┘
                            ▲                    ▲
              X-App-Id/Secret│                    │
                     ┌──────┴─────┐      ┌───────┴─────┐
                     │   MyChat   │      │   sql2er    │
                     └────────────┘      └─────────────┘
```

## MyChat 侧需要配什么

```bash
WECHAT_AUTH_ENABLED=true
WECHAT_AUTH_BASE_URL=http://127.0.0.1:8020   # 回环：应用侧接口不公网暴露
WECHAT_AUTH_APP_ID=mychat
WECHAT_AUTH_APP_SECRET=<取自 /etc/wxauth/apps.json 里 mychat 那条>
WECHAT_AUTH_LOGIN_QR_URL=/images/wechat-account-qrcode.jpg   # 关键词模式下的静态二维码
```

生产守卫：非 dev/test 环境下开了开关却没有 `APP_SECRET` → **后端拒绝启动**。
（否则表现是一堆莫名的 503，而"顺手把它关掉"是最糟的修法。）

## MyChat 侧的接口

| 端点 | 说明 |
|---|---|
| `GET /api/wechat/login-info` | 公开。登录页所需信息（`mode` / `qrcode_url` / `keyword`） |
| `GET /api/wechat/qrcode` | 代理带参数二维码图片，**同源**（页面不会嵌别家域名） |
| `POST /api/auth/login/wechat` | 用码换会话；新关注者自动建号 |
| `GET/POST/DELETE /api/auth/wechat/binding` | 绑定 / 解绑 / 查看 |

**绑定入口不能省**：否则既有账号（比如 admin）一扫码就会凭空多出一个空账号，登不回自己的号。
入口在「设置 → 账号安全」。

## 两种模式（取决于 wechat-auth 有没有配 AppID/AppSecret）

| | 关键词模式（默认） | 二维码模式 |
|---|---|---|
| 登录页显示 | 公众号账号二维码 + 「发送 mychat」 | **MyChat 专属的带参数二维码** |
| 已关注用户 | 需要在公众号发关键词 | **扫码即触发 SCAN 事件**，直接拿码 |
| 回复文案 | `您的 MyChat 验证码是：XXXXXX` | 同左 |

`login-info` 返回的 `mode` 字段决定登录页显示哪一种，**MyChat 前端无需改动**。

## 错误语义（别把这两类混在一起）

- 码错/过期/已用过/**不是为 MyChat 签发的** → `401`，「公众号验证码错误或已过期」
- wechat-auth 挂了、或我们的 `APP_SECRET` 不对 → `503`，「微信登录服务暂时不可用」

第二类混进第一类会让故障期间**每个用户都被告知"你的验证码错了"**，并把值班的人引向错误的方向。
两侧各有一条测试钉住这个区分。

## 排障

```bash
systemctl status wxauth                      # 服务本身
curl -s http://127.0.0.1:8020/api/v1/ready   # {"status":"ready"}
journalctl -u wxauth -f
journalctl -u mychat-backend -f | grep wechat
```

- `503 微信登录服务暂时不可用` → wechat-auth 不可达，或 `APP_SECRET` 与注册表不一致
- 登录页没有二维码、只有文字 → 正常（关键词模式）。看 `login-info` 的 `mode`
- 扫码后公众号没反应 → 公众号后台的服务器配置 URL / Token 不对，或微信消息没到 wxauth

## 历史

2026-09-17 之前这里跑的是「两个后端各自发码 + HMAC 派生 + nginx mirror」那一套，
设计记录见 `docs/superpowers/specs/2026-09-17-wechat-mp-login-design.md`。
那套复杂度全部来自「共用一个回调」，拆出中央服务后已被删除。
