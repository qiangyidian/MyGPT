/** 提示词库「放进输入框」这一侧的纯逻辑（不读表单、不发请求，所以能单测）。
 *
 * 分工：占位符插值、本地筛选/排序、表单校验在 `@/lib/prompt-apply`（设置页共用
 * 那份）。这里持有的是别处没有的几件小事中**仍在用**的那些 —— 目前只剩
 * ``extractVariables``：按 ``prompt-apply.ts`` 的 ``extractPlaceholders`` 同一口径
 * 识别占位符，但额外带上每个占位符的原文写法与下标（编辑表单要按它渲染「待填格子」
 * 的清单与位置）。
 *
 * 说明：原先还有「插进 textarea 光标处」与「从草稿反向生成表单初值」两组函数，
 * 它们唯一的调用方是输入框工具栏的提示词库入口；该入口已按产品决定移除（模板只在
 * 设置页管理），那两组随之删掉，不再留在库里等一个不存在的消费者。
 */
import { PLACEHOLDER_SOURCE, PROMPT_LIMITS } from "@/lib/prompt-apply";
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

/** 列表里的一行摘要：折叠空白再截断，免得整段正文撑爆卡片。 */
export function previewText(content: string, max = 80): string {
  const flat = content.replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, Math.max(0, max - 1))}…` : flat;
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
