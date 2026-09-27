/** 提示词库（条目 34）的纯逻辑：占位符识别 / 插值 + 本地筛选与排序。
 *
 * 刻意不碰 DOM，也不 import `api.ts`（那个模块在浏览器里缺
 * `NEXT_PUBLIC_API_BASE_URL` 时会直接抛错），所以能在 vitest 的
 * `environment: "node"` 下直接单测。
 *
 * 替换只发生在前端是有意的（见 `backend/app/models/prompt_template.py`）：服务端
 * 把变量值拼进模板再发给模型，等于开一条注入通道，而模板的价值恰恰是让用户看见
 * **将要发出去的确切文本**。所以这里既做替换，也如实报告「还缺哪几个占位符」。
 */
import type { PromptTemplate, PromptTemplateInput } from "./types";

/**
 * 请求体类型转发：让「表单 → 请求体」这条链上只认一个入口
 * （`promptCreateBody` / `promptPatchBody` 的返回类型就在这里），消费者不必同时
 * import `types.ts` 和 `prompt-apply.ts`。
 */
export type { PromptTemplate, PromptTemplateInput } from "./types";

/**
 * 占位符语法的唯一定义：`{{名称}}` 与 `${名称}`，同一个模板里可以混用（预置模板就是
 * 这么写的）。名字允许中文与空格（「目标读者」比 `target_audience` 更适合中文用户），
 * 但不允许换行与嵌套花括号，长度封顶 —— 否则正文里随手写的一段说明会被当成变量，
 * 「检测到 N 个占位符」的提示就全是噪音。
 *
 * `prompt-library.ts`（插入时选中第一个待填占位符）也从这里取同一份正则源：两边识别
 * 口径必须一致，否则弹窗说「3 个占位符」而输入框只选中了其中两个写法不同的那个。
 * 组 1 = `{{}}` 里的名字，组 2 = `${}` 里的名字。
 */
const PLACEHOLDER_NAME = String.raw`[\p{L}\p{N}_][\p{L}\p{N}_ -]{0,39}?`;

export const PLACEHOLDER_SOURCE =
  `\\{\\{\\s*(${PLACEHOLDER_NAME})\\s*\\}\\}|\\$\\{\\s*(${PLACEHOLDER_NAME})\\s*\\}`;

/** 新建一个带 `g` 的匹配器（`matchAll` / `replace` 都要新鲜的 lastIndex）。 */
export function placeholderRe(): RegExp {
  return new RegExp(PLACEHOLDER_SOURCE, "gu");
}

/** 占位符名去掉首尾空白后即为规范键；空的（`{{ }}`）不算占位符，原样留着。 */
function placeholderName(curly: string | undefined, dollar: string | undefined): string {
  return (curly ?? dollar ?? "").trim();
}

/**
 * 与服务端 schema 同源（`backend/app/schemas/prompt_template.py`）。改一边必须改
 * 另一边，否则表单填得进去、保存却 422。
 */
export const PROMPT_LIMITS = {
  title: { max: 128 },
  content: { max: 20000 },
  category: { max: 32 },
  description: { max: 500 },
  tags: { maxCount: 10, maxLen: 32 },
} as const;

/** 与 `app/models/prompt_template.py` 的列默认值一致。 */
export const DEFAULT_PROMPT_CATEGORY = "通用";

/**
 * react-query 键前缀。列表按 `[...前缀, scope, category]` 分片缓存，但任何写操作
 * 都整片失效——预置与「我的」同住一个接口（`scope=all` 时两边都有），只失效自己
 * 那一片会让选择器拿着过期数据。
 */
export const PROMPTS_QUERY_KEY = ["prompts"] as const;
export const PROMPT_CATEGORIES_QUERY_KEY = ["prompt-categories"] as const;

/** `user_id === null` = 平台预置：人人可读，只有管理员能改，所以列表要按归属分流。 */
export function isPresetPrompt(prompt: PromptTemplate): boolean {
  return prompt.user_id === null;
}

/** 归一化用于搜索的文本：小写 + 去掉所有空白（含全角空格）。 */
function normalizeForSearch(text: string): string {
  return text.toLowerCase().replace(/\s+/g, "");
}

/** 模板可搜索的文本：标题 / 描述 / 正文 / 标签拼成一段，缺字段就当空串。 */
function haystack(prompt: PromptTemplate): string {
  return normalizeForSearch(
    [
      prompt.title,
      prompt.description ?? "",
      prompt.content,
      (prompt.tags ?? []).join(" "),
    ].join("\n")
  );
}

/** 列表可见范围，与服务端 `scope` 同一套取值。 */
export type PromptScope = "all" | "mine" | "preset";

export interface PromptFilter {
  /** 自由文本：空格分词后逐词命中（中文没有词边界，所以是子串匹配）。 */
  q?: string;
  /** 精确分类（去空白后比较）；空串/未给 = 不按分类筛。 */
  category?: string;
  scope?: PromptScope;
}

/**
 * 客户端筛选，用于「已经拉到手里的那一页」—— 输入即筛，不必每敲一个字发一次请求。
 *
 * 词与词之间是 AND：`代码 审查` 要求两个词都命中同一行的任意字段。因为归一化会把
 * 两侧空白都去掉，`AI写作` 与 `AI 写作` 等价 —— 中文用户手滑多打一个空格不该得到
 * 空结果。
 */
export function searchFilter(
  prompts: readonly PromptTemplate[],
  filter: PromptFilter
): PromptTemplate[] {
  const scope = filter.scope ?? "all";
  const category = (filter.category ?? "").trim();
  const normalizedCategory = category ? normalizeForSearch(category) : "";
  const terms = (filter.q ?? "").trim().split(/\s+/).filter(Boolean).map(normalizeForSearch);

  return prompts.filter((prompt) => {
    if (scope === "mine" && isPresetPrompt(prompt)) return false;
    if (scope === "preset" && !isPresetPrompt(prompt)) return false;
    if (normalizedCategory && normalizeForSearch(prompt.category) !== normalizedCategory) {
      return false;
    }
    if (terms.length === 0) return true;
    const text = haystack(prompt);
    return terms.every((term) => text.includes(term));
  });
}

function byTimeDesc(a: string, b: string): number {
  const ta = Date.parse(a);
  const tb = Date.parse(b);
  // 解析不了的（脏数据 / 非 ISO）排到最后，而不是让 NaN 把整个排序变成随机数。
  if (Number.isNaN(ta) && Number.isNaN(tb)) return 0;
  if (Number.isNaN(ta)) return 1;
  if (Number.isNaN(tb)) return -1;
  return tb - ta;
}

/**
 * 分组展示顺序：平台预置在前（按 `sort_order` 升序，运营在库里改一行数据就能调整
 * 先后），用户自己的模板在后（最近更新的先出现）。
 *
 * 两个分组都带确定性兜底键（时间 → id）：`updated_at` 相同的行如果顺序乱抖，用户会
 * 看到列表每次刷新都在变。不改动入参数组。
 */
export function sortPromptGroups(prompts: readonly PromptTemplate[]): PromptTemplate[] {
  return [...prompts].sort((a, b) => {
    const presetA = isPresetPrompt(a);
    const presetB = isPresetPrompt(b);
    if (presetA !== presetB) return presetA ? -1 : 1;
    if (presetA) {
      if (a.sort_order !== b.sort_order) return a.sort_order - b.sort_order;
    } else {
      const updated = byTimeDesc(a.updated_at, b.updated_at);
      if (updated !== 0) return updated;
      const created = byTimeDesc(a.created_at, b.created_at);
      if (created !== 0) return created;
    }
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
}

/** 模板里需要用户填的占位符名，按首次出现顺序、去重。 */
export function extractPlaceholders(content: string): string[] {
  const seen = new Set<string>();
  const names: string[] = [];
  for (const match of content.matchAll(placeholderRe())) {
    const name = placeholderName(match[1], match[2]);
    if (!name || seen.has(name)) continue;
    seen.add(name);
    names.push(name);
  }
  return names;
}

export interface InterpolatedTemplate {
  /** 渲染结果：填了的占位符被替换，没填的**原样保留**（可见即安全，绝不静默删掉）。 */
  text: string;
  /** 模板仍然缺的占位符（出现顺序、去重）；空数组代表可以直接发出去。 */
  missing: string[];
  /** 模板一共需要的占位符（出现顺序、去重）。 */
  placeholders: string[];
}

/**
 * 用用户给的值替换 `{{占位符}}` / `${placeholder}`。
 *
 * `values` 的键是占位符名（已去首尾空白，区分大小写 —— 调用方用
 * `extractPlaceholders` 拿到名字再回填，天然对得上）。值整段 trim：粘贴多行代码时
 * 内部换行保留，只去掉用户顺手带上的外围空白。全空（`"   "`）算没填，占位符留在原位。
 */
export function interpolateTemplate(
  content: string,
  values: Readonly<Record<string, string>>
): InterpolatedTemplate {
  const placeholders = extractPlaceholders(content);
  const filled = new Map<string, string>();
  for (const name of placeholders) {
    const value = (values[name] ?? "").trim();
    if (value) filled.set(name, value);
  }
  const text = content.replace(placeholderRe(), (match, curly?: string, dollar?: string) => {
    const name = placeholderName(curly, dollar);
    const value = name ? filled.get(name) : undefined;
    return value === undefined ? match : value;
  });
  return {
    text,
    missing: placeholders.filter((name) => !filled.has(name)),
    placeholders,
  };
}

// ---------------------------------------------------------------------------
// 表单：设置页的新建 / 编辑用同一套纯校验，页面组件里不留规则。
// ---------------------------------------------------------------------------

/** 表单态：输入框一律留字符串，`tags` 是用户敲的原始串（提交时再切分）。 */
export interface PromptForm {
  title: string;
  content: string;
  category: string;
  description: string;
  tags: string;
}

export const EMPTY_PROMPT_FORM: PromptForm = {
  title: "",
  content: "",
  category: DEFAULT_PROMPT_CATEGORY,
  description: "",
  tags: "",
};

/** 编辑已有模板时的初始值（`null` = 新建）。 */
export function promptFormFromTemplate(
  prompt: PromptTemplate | null
): PromptForm {
  if (!prompt) return { ...EMPTY_PROMPT_FORM };
  return {
    title: prompt.title,
    content: prompt.content,
    category: prompt.category || DEFAULT_PROMPT_CATEGORY,
    description: prompt.description ?? "",
    tags: (prompt.tags ?? []).join("、"),
  };
}

/** 标签切分：中英文逗号 / 顿号 / 分号 / 空白都算分隔符，去空去重。 */
export function parseTags(raw: string): string[] {
  const out: string[] = [];
  for (const part of raw.split(/[,，、;；\s]+/)) {
    const tag = part.trim();
    if (tag && !out.includes(tag)) out.push(tag);
  }
  return out;
}

export type PromptFormErrors = Partial<Record<keyof PromptForm, string>>;

/** 返回字段 → 中文错误；有任一错误时调用方应禁用保存。 */
export function validatePromptForm(form: PromptForm): PromptFormErrors {
  const errors: PromptFormErrors = {};
  const title = form.title.trim();
  if (!title) errors.title = "请填写标题";
  else if (title.length > PROMPT_LIMITS.title.max) {
    errors.title = `标题最长 ${PROMPT_LIMITS.title.max} 个字符`;
  }
  if (!form.content.trim()) errors.content = "请填写提示词内容";
  else if (form.content.length > PROMPT_LIMITS.content.max) {
    errors.content = `内容最长 ${PROMPT_LIMITS.content.max} 个字符`;
  }
  const category = form.category.trim();
  if (!category) errors.category = "请填写分类";
  else if (category.length > PROMPT_LIMITS.category.max) {
    errors.category = `分类最长 ${PROMPT_LIMITS.category.max} 个字符`;
  }
  if (form.description.trim().length > PROMPT_LIMITS.description.max) {
    errors.description = `描述最长 ${PROMPT_LIMITS.description.max} 个字符`;
  }
  const tags = parseTags(form.tags);
  if (tags.length > PROMPT_LIMITS.tags.maxCount) {
    errors.tags = `最多 ${PROMPT_LIMITS.tags.maxCount} 个标签`;
  } else {
    const tooLong = tags.find((t) => t.length > PROMPT_LIMITS.tags.maxLen);
    if (tooLong) errors.tags = `单个标签最长 ${PROMPT_LIMITS.tags.maxLen} 个字符`;
  }
  return errors;
}

/** POST 请求体（服务端会清空白 / 去重，这里只做同样的清洗以免两边各说各话）。 */
export function promptCreateBody(form: PromptForm): PromptTemplateInput {
  const description = form.description.trim();
  return {
    title: form.title.trim(),
    content: form.content,
    category: form.category.trim() || DEFAULT_PROMPT_CATEGORY,
    tags: parseTags(form.tags),
    description: description || null,
  };
}

/**
 * PATCH 请求体：只发送真正改过的字段。
 *
 * 服务端按 `model_fields_set` 应用（省略 = 不动），所以「发什么」就是语义的一部分 ——
 * 把没改的字段一起发上去会把别人（或另一个标签页）刚存的价值抹掉。没改任何字段时返回
 * `null`，调用方据此连请求都不发。
 */
export function promptPatchBody(
  form: PromptForm,
  original: PromptTemplate
): Partial<PromptTemplateInput> | null {
  const next = promptCreateBody(form);
  const patch: Partial<PromptTemplateInput> = {};
  if (next.title !== original.title) patch.title = next.title;
  if (next.content !== original.content) patch.content = next.content;
  if (next.category !== original.category) patch.category = next.category;
  if (next.description !== original.description) patch.description = next.description;
  const originalTags = original.tags ?? [];
  const nextTags = next.tags ?? [];
  if (
    nextTags.length !== originalTags.length ||
    nextTags.some((tag, i) => tag !== originalTags[i])
  ) {
    patch.tags = nextTags;
  }
  return Object.keys(patch).length ? patch : null;
}
