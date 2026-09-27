import { describe, expect, it } from "vitest";

import { DIFF_MAX_LINES_PER_SIDE } from "@/lib/diff";
import {
  buildVersionDiff,
  describeVersion,
  isRestorable,
  isSameContent,
  originLabel,
  sortVersionsNewestFirst,
  versionNumber,
  VERSION_DIFF_DIRECTION,
  versionPreview,
  versionsForMessage,
  type MessageVersion,
  type VersionDiffView,
} from "../message-versions";

/** 联合类型三分支，用例只想看逐行那一支。 */
function expectDiff(view: VersionDiffView) {
  if (view.kind !== "diff") throw new Error(`期望逐行差异，实际是 ${view.kind}`);
  return view;
}

const AT = (iso: string) => iso;

function version(overrides: Partial<MessageVersion> = {}): MessageVersion {
  return {
    id: "v1",
    message_id: "m1",
    conversation_id: "c1",
    role: "assistant",
    content: "旧答案",
    metadata: {},
    model_name: "gpt-x",
    total_tokens: 12,
    cost_usd: null,
    origin: "regenerate",
    created_at: AT("2026-09-20T10:00:00Z"),
    ...overrides,
  };
}

describe("originLabel", () => {
  it("四种来源都有中文说法", () => {
    expect(originLabel("edit")).toBe("修改前");
    expect(originLabel("regenerate")).toBe("重新生成前");
    expect(originLabel("restore")).toBe("切回时被替换");
    expect(originLabel("truncate")).toBe("截断前");
  });

  it("后端加了新 origin 也不会把英文甩给用户", () => {
    expect(originLabel("future_reason")).toBe("历史版本");
  });
});

describe("versionsForMessage", () => {
  const current = {
    id: "m2",
    role: "assistant",
    created_at: AT("2026-09-20T11:00:00Z"),
  };

  it("只收本轮消息之前、角色一致的版本", () => {
    const rows = [
      version({ id: "a", created_at: AT("2026-09-20T10:00:00Z") }),
      version({ id: "b", created_at: AT("2026-09-20T12:00:00Z") }),
      version({ id: "c", role: "user", created_at: AT("2026-09-20T09:00:00Z") }),
    ];
    expect(versionsForMessage(rows, current).map((r) => r.id)).toEqual(["a"]);
  });

  it("时间戳坏掉的行不参与归属判断，也不抛异常", () => {
    const rows = [version({ id: "bad", created_at: "not-a-date" })];
    expect(versionsForMessage(rows, current)).toEqual([]);
    expect(versionsForMessage(rows, { ...current, created_at: "?" })).toEqual([]);
  });
});

describe("versionNumber", () => {
  it("按时间正序编号，界面展示则按新的在前", () => {
    const group = [
      version({ id: "new", created_at: AT("2026-09-20T12:00:00Z") }),
      version({ id: "old", created_at: AT("2026-09-20T10:00:00Z") }),
    ];
    expect(versionNumber(group, "old")).toBe(1);
    expect(versionNumber(group, "new")).toBe(2);
    expect(versionNumber(group, "unknown")).toBe(0);
    expect(sortVersionsNewestFirst(group).map((r) => r.id)).toEqual(["new", "old"]);
  });
});

describe("versionPreview", () => {
  it("折叠空白并按长度截断", () => {
    expect(versionPreview("一行\n多  行")).toBe("一行 多 行");
    expect(versionPreview("x".repeat(10), 5)).toBe(`${"x".repeat(4)}…`);
  });

  it("空正文不会抛异常", () => {
    expect(versionPreview("")).toBe("");
  });
});

describe("isSameContent / isRestorable", () => {
  it("内容与模型都一致才算同一版（切回按钮该禁用）", () => {
    expect(isSameContent(version({ content: "A", model_name: "m" }), { content: "A", model_name: "m" })).toBe(
      true
    );
    expect(isSameContent(version({ content: "A", model_name: "m" }), { content: "A", model_name: "n" })).toBe(
      false
    );
    expect(isSameContent(version({ content: "A" }), { content: "A" })).toBe(true);
  });

  it("空正文的历史版本不可切回", () => {
    expect(isRestorable(version({ content: "   " }))).toBe(false);
    expect(isRestorable(version({ content: "有内容" }))).toBe(true);
  });
});

describe("describeVersion", () => {
  it("拼出「第 N 版 · 来源 · 模型」一行", () => {
    const group = [version({ id: "v1", model_name: "deepseek" })];
    expect(describeVersion(group[0], group)).toBe("第 1 版 · 重新生成前 · deepseek");
  });

  it("没记录模型时不留空段", () => {
    const group = [version({ id: "v2", model_name: null })];
    expect(describeVersion(group[0], group)).toBe("第 1 版 · 重新生成前");
  });
});

describe("buildVersionDiff", () => {
  it("旧的一侧是历史版本：红色删除来自这一版，绿色新增来自当前正文", () => {
    // 方向反了的话界面上一片红绿会说反，这条就是钉住它。
    const view = expectDiff(buildVersionDiff("甲\n乙\n丙", "甲\n改过的乙\n丙"));
    expect(
      view.rows.filter((row) => row.kind === "del").map((row) => row.text)
    ).toEqual(["乙"]);
    expect(
      view.rows.filter((row) => row.kind === "add").map((row) => row.text)
    ).toEqual(["改过的乙"]);
  });

  it("逐行结果里带上行号，界面不用自己数", () => {
    const view = expectDiff(buildVersionDiff("只属于这一版", "只属于当前版"));
    expect(
      view.rows.map((row) => `${row.kind}:${row.oldNumber ?? "-"}/${row.newNumber ?? "-"}`)
    ).toEqual(["del:1/-", "add:-/1"]);
  });

  it("单侧超过规模上限时退化成摘要提示，不硬做逐行比对", () => {
    const big = Array.from(
      { length: DIFF_MAX_LINES_PER_SIDE + 1 },
      (_, i) => `第 ${i} 行`
    ).join("\n");
    const view = buildVersionDiff(big, "短当前正文");
    expect(view.kind).toBe("too-large");
    if (view.kind !== "too-large") throw new Error("期望退化");
    expect(view.note).toContain(String(DIFF_MAX_LINES_PER_SIDE));
    expect(view.note).toContain("未逐行比对");
  });

  it("刚好在上限内仍然逐行比对（退化条件是超过，不是达到）", () => {
    const atLimit = Array.from(
      { length: DIFF_MAX_LINES_PER_SIDE },
      (_, i) => `第 ${i} 行`
    ).join("\n");
    expect(buildVersionDiff(atLimit, "第 0 行").kind).toBe("diff");
  });

  it("内容一致、以及只差末尾换行，都归成「无差异」而不是画一片空白", () => {
    const same = buildVersionDiff("一样的内容", "一样的内容");
    expect(same.kind).toBe("unchanged");
    if (same.kind !== "unchanged") throw new Error("期望一致");
    expect(same.note).toContain("内容一致");

    const trailingNewline = buildVersionDiff("一行\n", "一行");
    expect(trailingNewline.kind).toBe("unchanged");
    if (trailingNewline.kind !== "unchanged") throw new Error("期望无行级差异");
    expect(trailingNewline.note).toContain("行级差异");
  });

  it("方向说明同时点名两侧，缺一句就会读反", () => {
    expect(VERSION_DIFF_DIRECTION).toContain("这一版");
    expect(VERSION_DIFF_DIRECTION).toContain("当前版本");
  });
});
