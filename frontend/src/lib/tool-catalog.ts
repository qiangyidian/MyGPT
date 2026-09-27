// 后台工具目录的纯逻辑（条目 34③）。vitest 是 `environment: "node"`，所以凡是要被
// 测的判断都放这里，组件只负责画。
import type { ToolInfo } from "@/lib/types";

/** 目录摘要文案里用到的计数。 */
export interface CatalogSummary {
  total: number;
  disabled: number;
}

/**
 * 一个工具当前是否可用。
 *
 * 缺字段按**可用**处理：`GET /api/tools`（用户侧那份）根本不带这个字段，而它的语义
 * 就是"这一份里的都能用"。把 undefined 当成停用会让那份目录整片灰掉。
 */
export function isToolEnabled(tool: Pick<ToolInfo, "enabled">): boolean {
  return tool.enabled !== false;
}

export function catalogSummary(tools: readonly Pick<ToolInfo, "enabled">[]): CatalogSummary {
  const disabled = tools.filter((tool) => !isToolEnabled(tool)).length;
  return { total: tools.length, disabled };
}

/**
 * 开关按下去之后告诉用户"还没全线生效"。
 *
 * 状态存在库里、各进程读的是带 15 秒 TTL 的本地快照（`app/services/tool_toggles.py`），
 * 所以写的那个进程立刻认账、其余进程最慢下个 tick。文案写成「已停用」会让人以为
 * 此刻已经没有人能跑它 —— 而他刚按下去的理由恰恰是"它正在闯祸"。
 */
export const TOGGLE_EFFECT_NOTE = "其余进程最长 15 秒内跟上（正在跑的这一次不会被打断）";

/** 停用一句工具时需要留下的东西：理由。 */
export function reasonRequiredFor(enabled: boolean): boolean {
  // 打开一个工具不需要理由（那是回到默认状态），关掉才需要 —— 见 set_enabled 的注释：
  // 一周后没人记得当初为什么停，重新打开就成了一次无人负责的赌博。
  return !enabled;
}

/** 停用/启用后的 toast 正文（不含"已生效"这种做不到的承诺）。 */
export function toggleResultMessage(name: string, enabled: boolean): string {
  return enabled ? `已重新启用 ${name}` : `已停用 ${name}`;
}

/**
 * 目录排序：停用的沉到最后，其次按分类、再按名字。
 *
 * 稳定排序是这里的全部要点 —— 运营来找的是"哪个被我关了"和"那个危险工具在哪"，
 * 而不是浏览注册顺序。分类参与排序是因为同名工具在不同分类里确实存在。
 */
export function sortCatalog<T extends Pick<ToolInfo, "name" | "category" | "enabled">>(
  tools: readonly T[]
): T[] {
  return [...tools].sort((a, b) => {
    const offA = isToolEnabled(a) ? 0 : 1;
    const offB = isToolEnabled(b) ? 0 : 1;
    if (offA !== offB) return offA - offB;
    const byCategory = String(a.category ?? "").localeCompare(String(b.category ?? ""));
    if (byCategory !== 0) return byCategory;
    return a.name.localeCompare(b.name);
  });
}
