/**
 * 模型选择器的两级菜单数据：模型配置 → 「供应商节点 + 该节点下的模型叶子」。
 *
 * 后端的 ModelConfig 是「一个接入点 + 一个模型」的一一对应（provider 字段是
 * openai-compatible / hermes 这类技术标签），所以第一级用用户自己命名的
 * ``name`` 分组——同名配置（同一供应商下加的多个模型）合并为一个节点。
 * 叶子显示名取 model_name；同节点下撞名时追加序号保证可区分。
 */
import type { ModelConfig } from "@/lib/types";

export interface ModelMenuLeaf {
  /** ModelConfig.id —— 真正提交给后端的值。 */
  id: string;
  /** 下拉里显示的名字（model_name，撞名加序号）。 */
  displayName: string;
}

export interface ModelMenuGroup {
  /** 第一级标题 = 配置的 name（用户按供应商命名）。 */
  providerName: string;
  models: ModelMenuLeaf[];
}

export function groupModelsForMenu(models: readonly ModelConfig[]): ModelMenuGroup[] {
  const byName = new Map<string, ModelConfig[]>();
  for (const m of models) {
    const key = m.name || m.provider;
    const bucket = byName.get(key);
    if (bucket) bucket.push(m);
    else byName.set(key, [m]);
  }

  return [...byName.entries()]
    .sort(([a], [b]) => a.localeCompare(b, "zh-Hans-CN"))
    .map(([providerName, configs]) => {
      // 同节点撞名（同名配置、同 model_name）加 1/2/… 序号，保证叶子可区分。
      const seen = new Map<string, number>();
      const leaves = configs.map((m) => {
        const base = m.model_name || providerName;
        const n = (seen.get(base) ?? 0) + 1;
        seen.set(base, n);
        return { id: m.id, displayName: n === 1 ? base : `${base} ${n}` };
      });
      return { providerName, models: leaves };
    });
}
