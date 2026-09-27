// 运营开关面板的纯逻辑：分组顺序、未知分组不许丢、"未生效"的计数口径。
import { describe, expect, it } from "vitest";

import {
  flagStatusText,
  groupFlags,
  groupLabel,
  inactiveFlags,
} from "@/lib/feature-flags";
import type { FeatureFlag } from "@/lib/types";

const flag = (over: Partial<FeatureFlag> & { key: string }): FeatureFlag => ({
  label: over.key,
  group: "engine",
  enabled: true,
  value: "true",
  source: "X",
  note: "",
  ...over,
});

describe("groupFlags", () => {
  it("按固定顺序出组，未知分组附在后面而不是被丢掉", () => {
    const groups = groupFlags([
      flag({ key: "exposure_2", group: "exposure" }),
      flag({ key: "engine_1", group: "engine" }),
      flag({ key: "brand_new", group: "quantum" }),
    ]);
    expect(groups.map((g) => g.group)).toEqual(["engine", "exposure", "quantum"]);
    // 后端加了新分组而前端没登记时，宁可标题难看也不能让人看不见这个开关。
    expect(groups[2].label).toBe("quantum");
  });

  it("空输入不炸，且不会凭空造出空分组", () => {
    expect(groupFlags([])).toEqual([]);
  });

  it("同组内保持后端给的顺序", () => {
    const groups = groupFlags([
      flag({ key: "b", group: "billing" }),
      flag({ key: "a", group: "billing" }),
    ]);
    expect(groups[0].items.map((f) => f.key)).toEqual(["b", "a"]);
  });
});

describe("inactiveFlags", () => {
  it("只数未生效的（全绿时不需要一句「一切正常」）", () => {
    const list = inactiveFlags([
      flag({ key: "on1" }),
      flag({ key: "off1", enabled: false }),
      flag({ key: "off2", enabled: false, group: "exposure" }),
    ]);
    expect(list.map((f) => f.key)).toEqual(["off1", "off2"]);
  });
});

describe("文案", () => {
  it("状态词与颜色同时携带信息（不许只靠颜色表达）", () => {
    expect(flagStatusText(flag({ key: "x" }))).toBe("生效中");
    expect(flagStatusText(flag({ key: "x", enabled: false }))).toBe("未生效");
  });

  it("已知分组有中文标题", () => {
    expect(groupLabel("engine")).toBe("多 Agent 引擎");
    expect(groupLabel("billing")).toBe("计费与配额");
  });
});
