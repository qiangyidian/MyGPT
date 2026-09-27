# @引用（知识库 / 文档 / 附件）—— 已落地的接线说明

这条能力跨了五层，任何一层各写一套「token 长什么样、上限是多少」都会散架。本文记录
**当前磁盘上的唯一口径**，改动前先对齐这里，别再新增第二份实现。

## 数据流

```
输入框敲 @            → useMentionPopover（防抖查询 + 键盘导航）
  ↓ api.searchMentions → GET /api/mentions（app/api/mentions.py）
选中一条              → insertMention（lib/mention-insert.ts）写回 token
正文里的 token        → collectMentions → ChatRequest.mentions（{kind,id}）
  ↓ ComposerSendOpts.mentions → app/page.tsx handleSend → useChatStream
                              → buildChatBody（lib/chat-request.ts）→ POST /api/chat/stream
```

token 留在正文里（`@[产品手册.pdf](doc:3f1a…)`），所以消息可复制、可导出、重新生成时
引用不丢；`mentions` 只是随请求发出去的机器字段，**不是**第二份状态源。

## 各层职责（只此一份，勿再造）

| 文件 | 负责 |
| --- | --- |
| `frontend/src/lib/inline-refs.ts` | token 的编解码：`TOKEN_SOURCE` 正则、`encodeRef` / `decodeToken` / `parseRefs` / `findMentionAt` / `deleteRefAt` / `removeRef` |
| `frontend/src/lib/mention-insert.ts` | 裁决层：上限从响应读取（`limitsFromMentionList`）、插入（`insertMention`）、发送前收敛（`collectMentions`）、被裁掉的中文提醒（`droppedNotice`） |
| `frontend/src/components/chat/mention-popover.tsx` | `useMentionPopover` + `MentionPopover`：查询、序号防迟到响应、不可检索目标的禁用说明 |
| `frontend/src/components/chat/mention-chips.tsx` | 输入框上方的引用一览，删 chip = 删正文里那枚 token |
| `backend/app/api/mentions.py` | 候选来源与权威上限：`items` + `truncated` + `max_knowledge_bases` + `max_mentions` |
| `backend/app/schemas/chat.py` | 请求体校验：`MAX_KB_PER_REQUEST = 5`、`MAX_MENTIONS_PER_REQUEST = 8`、`MENTION_KINDS = ("kb","doc","file")` |

## 三条必须保持同步的事实

1. **kind 只有短名** `kb` / `doc` / `file`。后端 `_token()` 与前端 `TOKEN_SOURCE` 都写死
   这三个；`refDisplayName` 负责把短名翻成中文标签。长名（`knowledge_base` 等）不属于协议。
2. **上限不在前端抄一份数字**。客户端预拒用响应里下发的 `max_*`（缺字段才回落到
   `FALLBACK_LIMITS`，其值与后端同源：8 / 5）。真正裁判仍是后端 —— 422 在 SSE 打开前抛出。
3. **输入框 `maxLength = COMPOSER_MAX_CHARS`（8000）**。一枚 token 约 53 字，所以这个数
   是为「满引用 + 中文正文」预留过的，不是随手取的整数；剩余不足 200 字才提示
   （`COMPOSER_NEAR_LIMIT`），常驻计数器只是噪音。

## 输入法与光标

合成期间（`compositionstart` → `compositionend`）不参与 `@` 判定：候选串会先落进
textarea，那段时间读到的「@拼音」不是用户想引用的东西。插入/删除引用后要写回光标，
而受控组件在 render 之后才更新 DOM，所以 composer 用 `pendingCaret` 存一拍，等
`value` 落地再对齐（见 `applyText` 与 `[value]` 那个 effect）。

## 校验

- 单测（纯函数，vitest `environment: "node"`，不碰 DOM）：
  `cd frontend && npx vitest run src/lib/__tests__/mention-insert.test.ts`
  —— 这份测试同时盯住 `inline-refs` 的编解码（插入结果要能被 `parseRefs` 读回）。
- 手工：敲 `@` 应弹候选（知识库 / 文档 / 本对话附件三分支）→ 选中后正文出现芯片 →
  引用一条「向量化中」的文档应被拒并给出中文原因 → 第 9 枚引用应被拒 →
  退格键应整枚删除而不是留下半截 `@[label`。
- 待补：`GET /api/mentions` 目前没有后端测试（`backend/tests/` 下无对应用例）。它是
  前端唯一的上限来源，值得覆盖「越权目标查不到」「未索引文档 selectable=false」
  「`truncated` 与 `max_*` 随响应下发」三条。
