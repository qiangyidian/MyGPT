// 项目改名 / 删除确认的表单逻辑（条目 30）。
//
// 校验文案与后端 `app/api/projects.py` 保持一致：前端拦一遍是为了不打无效请求，
// 但用户看到的句子必须和真被服务端拒时一模一样。
import { describe, expect, it } from "vitest";

import {
  DEFAULT_PROJECT_COLOR,
  PROJECT_NAME_MAX,
  normalizeProjectName,
  projectDeleteConsequences,
  projectDeleteConfirmed,
  projectRenamePatch,
  validateProjectColor,
  validateProjectName,
} from "@/lib/projects";
import type { Project, ProjectImpact } from "@/lib/types";

function impact(overrides: Partial<ProjectImpact> = {}): ProjectImpact {
  return {
    project_id: "p1",
    name: "季度报表",
    conversation_count: 0,
    archived_conversation_count: 0,
    message_count: 0,
    knowledge_base_count: 0,
    deletes_conversations: false,
    ...overrides,
  };
}

describe("validateProjectName", () => {
  it("rejects blank names with the server's message", () => {
    expect(validateProjectName("")).toBe("项目名称不能为空");
    expect(validateProjectName("   ")).toBe("项目名称不能为空");
  });

  it("rejects names longer than the column", () => {
    const msg = validateProjectName("名".repeat(PROJECT_NAME_MAX + 1));
    expect(msg).toContain(String(PROJECT_NAME_MAX));
  });

  it("accepts the boundary", () => {
    expect(validateProjectName("a".repeat(PROJECT_NAME_MAX))).toBeNull();
    expect(validateProjectName("  季度报表  ")).toBeNull();
  });
});

describe("normalizeProjectName / projectRenamePatch", () => {
  it("trims before anything hits the server", () => {
    expect(normalizeProjectName("  季度报表  ")).toBe("季度报表");
  });

  it("sends nothing for an unchanged or whitespace-only edit", () => {
    const p = { name: "季度报表" } as Pick<Project, "name">;
    expect(projectRenamePatch(p, "季度报表")).toBeNull();
    expect(projectRenamePatch(p, "  季度报表  ")).toBeNull();
  });

  it("sends nothing for an invalid draft instead of a broken request", () => {
    expect(projectRenamePatch({ name: "旧名" }, "   ")).toBeNull();
  });

  it("sends the trimmed new name only", () => {
    expect(projectRenamePatch({ name: "旧名" }, "  新名  ")).toEqual({ name: "新名" });
  });
});

describe("validateProjectColor", () => {
  it("accepts both hex shapes the server accepts", () => {
    expect(validateProjectColor("#abc")).toBeNull();
    expect(validateProjectColor("#A1B2C3")).toBeNull();
    expect(validateProjectColor(" #6366f1 ")).toBeNull();
  });

  it("rejects anything else in Chinese", () => {
    expect(validateProjectColor("blue")).toContain("#RRGGBB");
    expect(validateProjectColor("#12")).toContain("#RRGGBB");
    expect(validateProjectColor("")).toContain("颜色");
  });

  it("keeps the default in sync with the backend constant", () => {
    expect(DEFAULT_PROJECT_COLOR).toBe("#6366f1");
    expect(validateProjectColor(DEFAULT_PROJECT_COLOR)).toBeNull();
  });
});

describe("projectDeleteConsequences", () => {
  it("holds the dialog on a counting placeholder until the server answers", () => {
    const result = projectDeleteConsequences({ name: "季度报表" }, null);
    expect(result.lines).toEqual(["正在统计影响范围……"]);
    expect(result.destructive).toBe(false);
    expect(result.confirmation).toBe("季度报表");
  });

  it("states honestly that conversations survive as unassigned", () => {
    const result = projectDeleteConsequences(
      { name: "季度报表" },
      impact({ conversation_count: 3, message_count: 12 })
    );
    expect(result.destructive).toBe(false);
    expect(result.lines.join("\n")).toContain("未分组");
    expect(result.lines.join("\n")).toContain("3 个会话不会被删除");
    expect(result.lines.join("\n")).toContain("12 条消息全部保留");
    expect(result.lines.join("\n")).not.toContain("连带删除 3 个会话");
  });

  it("mentions archived conversations separately when there are any", () => {
    const result = projectDeleteConsequences(
      { name: "p" },
      impact({ conversation_count: 4, archived_conversation_count: 2, message_count: 30 })
    );
    expect(result.lines.join("\n")).toContain("2 个会话处于归档状态");
  });

  it("says nothing is filed when the project is empty", () => {
    const result = projectDeleteConsequences({ name: "p" }, impact());
    expect(result.lines.some((l) => l.includes("该项目下没有会话"))).toBe(true);
    expect(result.destructive).toBe(false);
  });

  it("flips to the destructive wording the moment the backend cascades", () => {
    // 今天 project_id 是软引用所以不会走到这里；哪天加了 FK 或改成级联删除，
    // 确认框必须立刻改口，而不是继续承诺「会话不会被删除」。
    const result = projectDeleteConsequences(
      { name: "p" },
      impact({
        conversation_count: 5,
        archived_conversation_count: 1,
        message_count: 40,
        deletes_conversations: true,
      })
    );
    expect(result.destructive).toBe(true);
    expect(result.lines[0]).toContain("连带删除 5 个会话");
    expect(result.lines[0]).toContain("40 条消息");
    expect(result.lines.join("\n")).toContain("1 个会话处于归档状态");
  });

  it("only warns about knowledge bases if a link ever exists", () => {
    const withKb = projectDeleteConsequences({ name: "p" }, impact({ knowledge_base_count: 2 }));
    expect(withKb.lines.join("\n")).toContain("连带删除 2 个知识库");
    const withoutKb = projectDeleteConsequences({ name: "p" }, impact());
    expect(withoutKb.lines.join("\n")).toContain("知识库不归属项目");
  });
});

describe("projectDeleteConfirmed", () => {
  it("requires the exact project name, ignoring outer whitespace", () => {
    expect(projectDeleteConfirmed("季度报表", "  季度报表 ")).toBe(true);
    expect(projectDeleteConfirmed("季度报表", "季度报")).toBe(false);
    expect(projectDeleteConfirmed("季度报表", "")).toBe(false);
  });

  it("never lets a blank name be confirmed by a blank input", () => {
    expect(projectDeleteConfirmed("   ", "")).toBe(false);
    expect(projectDeleteConfirmed("   ", "x")).toBe(false);
  });
});
