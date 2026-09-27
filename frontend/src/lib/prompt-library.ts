/** 提示词库「放进输入框」这一侧的纯逻辑（不读表单、不发请求，所以能单测）。
 *
 * 分工：占位符插值、本地筛选/排序、表单校验在 `@/lib/prompt-apply`（选择弹窗与设置
 * 页共用那份）。这里只管两件别处没有的事 ——
 *
 * 1. 把一段模板放进 textarea 的**光标处**，并把光标落到第一个待填占位符上（错一点
 *    就很难看：光标乱跳、模板挤进半句话中间，而它跟界面毫无关系，所以配一份 vitest）；
 * 2. 从输入框草稿反向生成「存为模板」的表单初值（标题按首行猜、副本标题加后缀）。
 */
import {
  EMPTY_PROMPT_FORM,
  PLACEHOLDER_SOURCE,
  PROMPT_LIMITS,
  type PromptForm,
} from "@/lib/prompt-apply";
import type { PromptTemplate, PromptTemplateInput } from "@/lib/types";

export type VariableSyntax = "curly" | "dollar";

export interface PromptVariable {
  /** 变量名，已去掉两端空白（例如 `文风`）。 */
  name: string;
  /** 原文里的写法（例如 `{{文风}}`）：插入后要选中的就是这一段。 */
  raw: string;
  syntax: VariableSyntax;
  /** 在原文中的起始下标。 */
  index: number;
}

export interface TextRange {
  start: number;
  end: number;
}

export interface InsertResult {
  /** 插入后的完整文本。 */
  value: string;
  /** 插入后光标应落在的下标。 */
  caret: number;
  /** 第一个待填占位符的区间；没有占位符时为 null。 */
  selection: TextRange | null;
}

/**
 * 占位符：``${名字}`` 与 ``{{名字}`` 两种写法都认（预置模板就是混着写的）。
 *
 * 名字允许中文与空格（「目标读者」比「target_audience」更适合中文用户），但不允许
 * 换行与嵌套花括号，长度也封了顶 —— 否则正文里随手写的一段说明会被当成变量，
 * 「检测到 N 个占位符」的提示就全是噪音。识别口径**与 ``prompt-apply.ts`` 的
 * ``extractPlaceholders`` 同源**（共用一份正则源），只是这里多带位置信息。
 */
const VARIABLE_RE = new RegExp(PLACEHOLDER_SOURCE, "gu");

/** 抽出占位符（按首次出现顺序；同名只留第一次，与 ``extractPlaceholders`` 同一口径）。 */
export function extractVariables(content: string): PromptVariable[] {
  const found: PromptVariable[] = [];
  const seen = new Set<string>();
  for (const match of content.matchAll(VARIABLE_RE)) {
    // 组 1 = `{{}}` 里的名字，组 2 = `${}` 里的名字（见 PLACEHOLDER_SOURCE）。
    const curly = match[1];
    const name = (curly ?? match[2] ?? "").trim();
    // 同名不同写法（``{{a}}`` 与 ``${a}``）在用户眼里是同一个要填的格子，所以
    // 去重只看名字 —— 填两次没人觉得理所当然。
    if (!name || seen.has(name)) continue;
    seen.add(name);
    found.push({
      name,
      raw: match[0],
      syntax: curly === undefined ? "dollar" : "curly",
      index: match.index ?? 0,
    });
  }
  return found;
}

/** 按写法还原占位符原文。 */
export function formatVariable(name: string, syntax: VariableSyntax = "curly"): string {
  return syntax === "dollar" ? `\${${name}}` : `{{${name}}}`;
}

function clampIndex(value: number, length: number): number {
  if (!Number.isFinite(value)) return length;
  return Math.min(Math.max(Math.trunc(value), 0), length);
}

/**
 * 用 ``insertion`` 替换 ``value`` 的 ``[start, end)``（``start === end`` 就是纯插入）。
 *
 * 下标先夹紧：textarea 的 selectionStart 在程序改过 value 之后会短暂地比长度大，
 * 直接 slice 得到的是错误结果而不是异常，那种 bug 最难查。
 */
export function insertAtCaret(
  value: string,
  start: number,
  end: number,
  insertion: string,
): { value: string; caret: number } {
  const from = clampIndex(start, value.length);
  const to = Math.max(from, clampIndex(end, value.length));
  return {
    value: value.slice(0, from) + insertion + value.slice(to),
    caret: from + insertion.length,
  };
}

/**
 * 把模板插进输入框：有选区就替换选中的文字，否则落在光标处；没给光标就接在末尾。
 *
 * 只在「会挤在一起」时补空行（前面不是行尾、后面不是行首），所以连着插两个模板不会
 * 长出一串空行。返回的 ``selection`` 交给 ``setSelectionRange``：第一个占位符整段选中，
 * 用户直接打字就能覆盖掉 ``{{文风}}``。
 */
export function insertTemplate(
  current: string,
  template: string,
  at?: TextRange,
): InsertResult {
  const from = clampIndex(at?.start ?? current.length, current.length);
  const to = Math.max(from, clampIndex(at?.end ?? from, current.length));
  const before = current.slice(0, from);
  const after = current.slice(to);
  const leading = before.length > 0 && !before.endsWith("\n") ? "\n\n" : "";
  const trailing = after.length > 0 && !after.startsWith("\n") ? "\n\n" : "";
  const value = before + leading + template + trailing + after;
  // 模板起点在最终文本里的位置（leading 的空行插在它前面）。
  const offset = before.length + leading.length;
  const first = extractVariables(template)[0];
  if (!first) {
    return { value, caret: offset + template.length, selection: null };
  }
  const start = offset + first.index;
  return {
    value,
    caret: start,
    selection: { start, end: start + first.raw.length },
  };
}

/**
 * 把插入结果落到 textarea 上：聚焦 + 选中第一个待填占位符。
 *
 * 调用方先更新自己的受控 value（``setValue(result.value)``）再调这里；下一帧是为了
 * 等 React 把新值写进 DOM —— 否则 selectionRange 会被随后的 value 复位到末尾。输入框
 * 若带自适应高度，记得在 setValue 之后顺手量一次（composer 的 handleInput）。
 */
export function applyInsertionToTextarea(
  el: HTMLTextAreaElement | null,
  result: InsertResult,
): void {
  if (!el) return;
  const start = result.selection?.start ?? result.caret;
  const end = result.selection?.end ?? result.caret;
  requestAnimationFrame(() => {
    el.focus();
    el.setSelectionRange(start, end);
  });
}

/** 列表里的一行摘要：折叠空白再截断，免得整段正文撑爆卡片。 */
export function previewText(content: string, max = 80): string {
  const flat = content.replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, Math.max(0, max - 1))}…` : flat;
}

/** 从草稿首行猜个标题：让用户少填一格，但随时能改。 */
export function titleFromDraft(
  draft: string,
  max: number = PROMPT_LIMITS.title.max,
): string {
  const line = draft
    .split("\n")
    .map((row) => row.trim())
    .find((row) => row.length > 0);
  const cleaned = (line ?? "").replace(/^[#>\-*\d.、\s]+/, "").trim();
  if (!cleaned) return "";
  return cleaned.length > max ? `${cleaned.slice(0, max - 1)}…` : cleaned;
}

/** 把输入框里的草稿存成新模板：标题按首行猜一个，分类落在默认值上先让用户能存。 */
export function promptFormFromDraft(draft: string): PromptForm {
  return { ...EMPTY_PROMPT_FORM, title: titleFromDraft(draft), content: draft };
}

/** 副本标题：加后缀，免得和原来的预置分不清；先按上限截断再接后缀。 */
export function copyTitle(title: string): string {
  const suffix = "（副本）";
  const base = title.trim() || "未命名模板";
  const budget = Math.max(0, PROMPT_LIMITS.title.max - suffix.length);
  return (base.length > budget ? base.slice(0, budget) : base) + suffix;
}

/** 「另存为我的模板」的请求体：内容照抄，标题带副本后缀（编辑表单初值走
 * ``promptFormFromTemplate``，两边不必各写一份）。 */
export function duplicateAsOwnInput(item: PromptTemplate): PromptTemplateInput {
  return {
    title: copyTitle(item.title),
    content: item.content,
    category: item.category,
    tags: [...item.tags],
    description: item.description,
  };
}
