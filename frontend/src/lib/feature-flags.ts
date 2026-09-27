// 运营开关面板的纯逻辑（条目 34④）。vitest 是 node 环境，所以分组与"该看一眼"的
// 判定放这里，组件只画。
import type { FeatureFlag } from "@/lib/types";

/** 分组顺序也是人读顺序：先问"我的引擎走没走"，再问"钱挡不挡"，最后才是暴露面。 */
export const FLAG_GROUP_ORDER = [
  "engine",
  "billing",
  "tools",
  "rag",
  "access",
  "exposure",
] as const;

const GROUP_LABELS: Record<string, string> = {
  engine: "多 Agent 引擎",
  billing: "计费与配额",
  tools: "工具与执行面",
  rag: "检索",
  access: "登录与语音",
  exposure: "对外暴露面",
};

export function groupLabel(group: string): string {
  // 后端新加一个分组时前端不许把它藏起来：原样把 key 当标题，至少看得见。
  return GROUP_LABELS[group] ?? group;
}

export interface FlagGroup {
  group: string;
  label: string;
  items: FeatureFlag[];
}

/** 按固定顺序分组；未知分组不丢，排在已知组之后。 */
export function groupFlags(flags: readonly FeatureFlag[]): FlagGroup[] {
  const byGroup = new Map<string, FeatureFlag[]>();
  for (const flag of flags) {
    const list = byGroup.get(flag.group);
    if (list) list.push(flag);
    else byGroup.set(flag.group, [flag]);
  }
  const known = FLAG_GROUP_ORDER.filter((g) => byGroup.has(g));
  const knownSet: ReadonlySet<string> = new Set(known);
  // 后端加了新分组而这里没登记时，按 key 字典序附在后面：宁可顺序不完美，也不能让
  // 一个开关因为前端少写一行而**不显示**。
  const extras = [...byGroup.keys()].filter((g) => !knownSet.has(g)).sort();
  return [...known, ...extras].map((group) => ({
    group,
    label: groupLabel(group),
    items: byGroup.get(group) ?? [],
  }));
}

/**
 * "配了但没生效"的那几项 —— 面板顶上的提醒只数这些。
 *
 * 只数未生效的，不数已生效的：全绿的时候没人需要一句"一切正常"，而运营真正会踩的坑
 * 是"我以为引擎在跑"。所以这一句的措辞必须指向**下一步做什么**（改哪个环境变量）。
 */
export function inactiveFlags(flags: readonly FeatureFlag[]): FeatureFlag[] {
  return flags.filter((flag) => !flag.enabled);
}

/** 一行结论的视觉语义（不要靠颜色单独承载信息，文案里也带着）。 */
export function flagStatusText(flag: FeatureFlag): string {
  return flag.enabled ? "生效中" : "未生效";
}
