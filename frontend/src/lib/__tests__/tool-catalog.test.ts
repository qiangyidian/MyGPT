// 工具目录的纯逻辑：启停口径、计数与排序。组件不测（vitest 是 node 环境，没有 jsdom）。
import { describe, expect, it } from "vitest";

import {
  catalogSummary,
  isToolEnabled,
  reasonRequiredFor,
  sortCatalog,
  toggleResultMessage,
  TOGGLE_EFFECT_NOTE,
} from "@/lib/tool-catalog";

type Row = { name: string; category: string; enabled?: boolean };

const row = (over: Partial<Row> & { name: string }): Row => ({ category: "general", ...over });

describe("isToolEnabled", () => {
  it("缺 enabled 字段按可用处理（用户侧目录根本不带这个字段）", () => {
    expect(isToolEnabled({})).toBe(true);
    expect(isToolEnabled({ enabled: true })).toBe(true);
    expect(isToolEnabled({ enabled: false })).toBe(false);
    // 写成 `!tool.enabled` 的那份实现会把第一种情况判成停用 —— 用户侧目录整片灰掉。
    expect(isToolEnabled({ enabled: undefined })).toBe(true);
  });
});

describe("catalogSummary", () => {
  it("只数真正被停用的，缺字段不算", () => {
    expect(
      catalogSummary([row({ name: "a" }), row({ name: "b", enabled: false }), row({ name: "c" })])
    ).toEqual({ total: 3, disabled: 1 });
  });
});

describe("sortCatalog", () => {
  it("停用的沉到最后，其余先按分类再按名字", () => {
    const sorted = sortCatalog([
      row({ name: "zz", category: "web" }),
      row({ name: "off", enabled: false, category: "aaa" }),
      row({ name: "aa", category: "general" }),
      row({ name: "ab", category: "general" }),
    ]).map((r) => r.name);
    expect(sorted).toEqual(["aa", "ab", "zz", "off"]);
  });

  it("不改传入数组（React 里就地排序会叫渲染结果与缓存不一致）", () => {
    const input = [row({ name: "b" }), row({ name: "a" })];
    const sorted = sortCatalog(input);
    expect(input.map((r) => r.name)).toEqual(["b", "a"]);
    expect(sorted.map((r) => r.name)).toEqual(["a", "b"]);
  });
});

describe("停用的取舍", () => {
  it("关要理由、开不要", () => {
    expect(reasonRequiredFor(false)).toBe(true);
    expect(reasonRequiredFor(true)).toBe(false);
  });

  it("开关文案不许承诺「已全线生效」", () => {
    expect(toggleResultMessage("web_search", false)).toBe("已停用 web_search");
    expect(toggleResultMessage("web_search", true)).toBe("已重新启用 web_search");
    // 生效是各进程各自跟上快照的：这句话必须出现在面板上，否则运营会以为按下去就
    // 立刻掐断了正在跑的那一次。
    expect(TOGGLE_EFFECT_NOTE).toContain("15 秒");
    expect(TOGGLE_EFFECT_NOTE).not.toContain("已生效");
  });
});
