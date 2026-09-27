/**
 * 消息版本历史（条目 31）的纯逻辑。
 *
 * 放这里而不是写在组件里，是因为这几件事全都是「判断」而不是「渲染」：一条历史版
 * 本能不能切回、它属于哪一轮、界面上该怎么称呼它。这些判断在 node 环境下可测，
 * 而本仓库的 vitest 没有 jsdom——组件里的 DOM 部分本来就测不了。
 *
 * 与后端 :class:`app.schemas.message_version.MessageVersionOut` 同形。
 */

import {
  DIFF_MAX_LINES_PER_SIDE,
  diffLines,
  diffTooLarge,
  type DiffRow,
} from "@/lib/diff";

export type VersionOrigin = "edit" | "regenerate" | "restore" | "truncate";

export interface MessageVersion {
  id: string;
  message_id: string;
  conversation_id: string;
  role: string;
  content: string;
  metadata: Record<string, unknown>;
  model_name: string | null;
  total_tokens: number | null;
  cost_usd: number | null;
  origin: string;
  created_at: string;
}

/** 后端 origin 取值；未知值原样回退到通用说法，不给英文。 */
export const VERSION_ORIGINS: readonly VersionOrigin[] = [
  "edit",
  "regenerate",
  "restore",
  "truncate",
];

const ORIGIN_LABELS: Record<VersionOrigin, string> = {
  edit: "修改前",
  regenerate: "重新生成前",
  restore: "切回时被替换",
  truncate: "截断前",
};

/** 这一版是「怎么没的」。 */
export function originLabel(origin: string): string {
  return ORIGIN_LABELS[origin as VersionOrigin] ?? "历史版本";
}

/**
 * 把历史版本挂到当前这条消息上。
 *
 * 后端按会话返回版本（重新生成会换掉消息行的 id，按 message_id 查恰好会漏掉最
 * 主要的那批），所以归属只能按时间判断：只有**早于当前消息**、且角色一致的版本
 * 才属于这一轮，晚于它的属于后面几轮。
 */
export function versionsForMessage(
  rows: readonly MessageVersion[],
  message: { id: string; role: string; created_at: string }
): MessageVersion[] {
  const boundary = Date.parse(message.created_at);
  if (!Number.isFinite(boundary)) return [];
  return rows.filter((row) => {
    const at = Date.parse(row.created_at);
    if (!Number.isFinite(at) || at >= boundary) return false;
    return row.role === message.role;
  });
}

/** 同一轮内按时间正序的序号（第 1 版 = 最早那次被换下来的内容）。 */
export function versionNumber(
  group: readonly MessageVersion[],
  versionId: string
): number {
  const ordered = [...group].sort((a, b) => a.created_at.localeCompare(b.created_at));
  const index = ordered.findIndex((row) => row.id === versionId);
  return index < 0 ? 0 : index + 1;
}

/** 列表按「新的在前」展示，与后端一致；无时间戳的行不参与排序。 */
export function sortVersionsNewestFirst(
  rows: readonly MessageVersion[]
): MessageVersion[] {
  return [...rows].sort((a, b) => b.created_at.localeCompare(a.created_at));
}

/** 预览：折叠空白 + 截断，历史列表里不放整篇回答。 */
export function versionPreview(content: string, limit = 120): string {
  const collapsed = (content ?? "").replace(/\s+/g, " ").trim();
  if (collapsed.length <= limit) return collapsed;
  return `${collapsed.slice(0, limit - 1)}…`;
}

/** 当前内容与某版一致时切回是空操作，界面据此禁用按钮。 */
export function isSameContent(
  version: MessageVersion,
  current: { content: string; model_name?: string | null }
): boolean {
  return (
    (version.content ?? "") === (current.content ?? "") &&
    (version.model_name ?? "") === (current.model_name ?? "")
  );
}

/** 有正文才谈得上切回；空版本（生成中断）留着只是噪声。 */
export function isRestorable(version: MessageVersion): boolean {
  return (version.content ?? "").trim().length > 0;
}

/** 一行说明：第 N 版 · 重新生成前 · 模型 · 时间片段。 */
export function describeVersion(
  version: MessageVersion,
  group: readonly MessageVersion[]
): string {
  const bits = [`第 ${versionNumber(group, version.id)} 版`, originLabel(version.origin)];
  if (version.model_name) bits.push(version.model_name);
  return bits.join(" · ");
}

/** 「这一版 vs 当前正文」在界面上的三种下场。 */
export type VersionDiffView =
  | { kind: "diff"; rows: DiffRow[] }
  | { kind: "unchanged"; note: string }
  | { kind: "too-large"; note: string };

/**
 * 把增删色块读反比看不到更糟，所以「谁是旧内容」只在这一处定一次，组件照着说。
 */
export const VERSION_DIFF_DIRECTION =
  "对比方向：这一版（旧）→ 当前版本（新）；绿色是当前版本新增的行，红色是这一版被改掉的行";

/**
 * 某条历史版本相对当前正文的差异。
 *
 * 行数统计不在这里做：`MarkdownDiff` 自己会报「新增 N 行 · 删除 M 行」，同一个数
 * 字算两遍迟早会对不上。这里只回答「该不该逐行比」以及「比不出东西时说什么」。
 */
export function buildVersionDiff(
  versionContent: string,
  currentContent: string
): VersionDiffView {
  const oldText = versionContent ?? "";
  const newText = currentContent ?? "";
  if (oldText === newText) {
    return { kind: "unchanged", note: "这一版与当前版本内容一致" };
  }
  if (diffTooLarge(oldText, newText)) {
    return {
      kind: "too-large",
      note: `这一版或当前版本超过 ${DIFF_MAX_LINES_PER_SIDE} 行，未逐行比对差异，下面只给摘要`,
    };
  }
  const rows = diffLines(oldText, newText);
  // 只差一个末尾换行时逐行比出来是「零增零删」，画出来一片空白，不如直接说清楚。
  if (!rows.some((row) => row.kind !== "equal")) {
    return { kind: "unchanged", note: "这一版与当前版本没有行级差异" };
  }
  return { kind: "diff", rows };
}
