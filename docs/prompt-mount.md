# 提示词库（条目 34）—— 已落地的接线说明

一套模板要能在三处被同一份规则读写：选择器弹窗、设置页、输入框。所以规则只写一份，
界面各自只负责摆放。本文记录**当前磁盘上的口径**。

## 两份纯逻辑的分工（刻意分开，不是重复）

| 文件 | 负责 | 为什么单独一份 |
| --- | --- | --- |
| `frontend/src/lib/prompt-apply.ts` | 占位符识别与替换（`PLACEHOLDER_SOURCE` / `extractPlaceholders` / `interpolateTemplate`）、本地筛选与分组排序、表单校验、请求体（`promptCreateBody` / `promptPatchBody`）、react-query 键 | 「将要发出去的确切文本」在这里定型；弹窗与管理页共用同一套校验 |
| `frontend/src/lib/prompt-library.ts` | 把模板插进 textarea 的**光标处**并选中第一个待填占位符（`insertTemplate` / `applyInsertionToTextarea`）、草稿→表单初值（`promptFormFromDraft` / `copyTitle` / `duplicateAsOwnInput`） | 这一半是 DOM 周边但仍是纯计算，所以配单测而不是靠手点 |

**占位符正则只有 `PLACEHOLDER_SOURCE` 一份**（`prompt-apply.ts`），`prompt-library.ts`
的 `VARIABLE_RE` 由它构造。两边各写一条的后果：弹窗说「3 个占位符」，输入框却只选中
了其中写法不同的那一个。写法支持 `{{名字}}` 与 `${名字}`，名字可含中文、空格、连字符，
长度封顶 40 —— 正文里随手写的说明不该被当成变量。

替换**只发生在前端**是有意的（见 `backend/app/models/prompt_template.py`）：服务端把变量
值拼进模板再发给模型，等于多开一条注入通道，而模板的价值恰恰是让用户看见最终文本。

## 挂载点

- 工具栏「提示词库」按钮：`frontend/src/components/chat/composer-toolbar.tsx`，一个
  DropdownMenu 两项 —— 打开选择器 / 把当前选中的多行文字存为模板。它不参与
  `isHermes` 隐藏：提示词库与对话模式无关。
- 插入与保存：`frontend/src/components/composer.tsx` 的 `insertPromptText`（走
  `insertTemplate` + `applyInsertionToTextarea`，所以插入后光标停在第一个 `{{城市}}` 上、
  直接打字即可覆盖）与 `saveSelection`（`promptFormFromDraft` 猜标题 → `api.createPrompt`）。
- 管理页：`frontend/src/app/settings/prompts/page.tsx`，已注册进
  `frontend/src/app/settings/layout.tsx` 侧栏。
- 取数一律走 `@/lib/api`：`api.listPrompts` / `listPromptCategories` / `getPrompt` /
  `createPrompt` / `updatePrompt`（PATCH 语义：省略 = 不动）/ `deletePrompt`。曾经存在的
  临时绕行层 `lib/prompt-api.ts` 与第二套出口 `promptsApi` 都已删除，不要再加回来。

## 上下文字段不能各自表述

- 分类不是前端常量表：`GET /api/prompts/categories` 按用量返回，界面只做筛选标签。
- `scope` 三取值 `all | mine | preset`，与服务端同一套；预置 = `user_id IS NULL`，
  只有管理员能改（`backend/app/api/prompts.py`）。
- 任何写操作都整片失效 `PROMPTS_QUERY_KEY` / `PROMPT_CATEGORIES_QUERY_KEY`：列表按
  `[前缀, scope, category]` 分片缓存，只失效自己那一片会让选择器拿着过期数据。
- 表单上限 `PROMPT_LIMITS` 与 `backend/app/schemas/prompt_template.py` 同源，改一边必须
  改另一边，否则填得进去、保存 422。
- 预置模板由迁移 `backend/migrations/versions/0018_prompt_templates.py` 落库（当前 9 条，
  覆盖写作 / 翻译 / 编程 / 办公 / 学习 / 分析 / 营销）。运营要改文案不必发版，直接改行
  数据即可 —— 分类同理，界面标签来自 `GET /api/prompts/categories` 而不是常量表。

## 校验

- 单测：`cd frontend && npx vitest run src/lib/__tests__/prompt-apply.test.ts src/lib/__tests__/prompt-library.test.ts`
- 后端契约：`cd backend && python -m pytest tests/test_prompts_api.py`
- 手工：`/settings/prompts` 建一个带 `{{城市}}` 的模板 → 输入框旁「提示词库」→ 我的模板
  里选它 → 不填时「插入到输入框」应为禁用，填完才能插入且预览已替换 → 插入后光标应选中
  `{{城市}}`，直接打字可覆盖。
