// 提示词库的纯逻辑（条目 34）。占位符替换只在客户端发生，服务端刻意不做
// （`app/models/prompt_template.py`），所以「哪一段文本会发出去」这件事完全由这里
// 决定 —— 它必须能被逐条钉住，而不是等到集成测试才发现替换漏了一个语法。
import { describe, expect, it } from "vitest";

import {
  DEFAULT_PROMPT_CATEGORY,
  EMPTY_PROMPT_FORM,
  PROMPT_LIMITS,
  extractPlaceholders,
  interpolateTemplate,
  isPresetPrompt,
  parseTags,
  promptCreateBody,
  promptFormFromTemplate,
  promptPatchBody,
  searchFilter,
  sortPromptGroups,
  validatePromptForm,
  type PromptForm,
} from "@/lib/prompt-apply";
import type { PromptTemplate } from "@/lib/types";

function template(
  overrides: Partial<PromptTemplate> & { id: string }
): PromptTemplate {
  return {
    user_id: "user-1",
    title: "标题",
    content: "内容",
    category: DEFAULT_PROMPT_CATEGORY,
    tags: [],
    description: null,
    sort_order: 0,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    ...overrides,
  };
}

const PRESET = (id: string, sort_order: number): PromptTemplate =>
  template({ id, user_id: null, title: `预置 ${id}`, sort_order });

function form(overrides: Partial<PromptForm> = {}): PromptForm {
  return { ...EMPTY_PROMPT_FORM, ...overrides };
}

describe("extractPlaceholders", () => {
  it("recognises both syntaxes in first-occurrence order", () => {
    expect(
      extractPlaceholders("文风：{{文风}}\n目标读者：${读者}\n再来一次：{{文风}}")
    ).toEqual(["文风", "读者"]);
  });

  it("trims the name it reports", () => {
    expect(extractPlaceholders("{{ 语言 }} 与 ${ 代码 }")).toEqual(["语言", "代码"]);
  });

  it("ignores empty and unbalanced braces", () => {
    expect(extractPlaceholders("{{ }} ${ } 半截的 {{foo 和 ${bar}")).toEqual(["bar"]);
  });

  it("returns nothing for plain text", () => {
    expect(extractPlaceholders("把这段话翻译成英文")).toEqual([]);
  });
});

describe("interpolateTemplate", () => {
  it("replaces every occurrence of both syntaxes", () => {
    const content = "{{语言}} 与 ${语言} 都要换：{{代码}}";
    const out = interpolateTemplate(content, { 语言: "Python", 代码: "print(1)" });
    expect(out.text).toBe("Python 与 Python 都要换：print(1)");
    expect(out.missing).toEqual([]);
  });

  it("leaves unfilled placeholders verbatim and reports them in order", () => {
    const out = interpolateTemplate("${读者} 先看 {{文风}} 再看 ${读者}", {
      读者: "财务同事",
    });
    expect(out.text).toBe("财务同事 先看 {{文风}} 再看 财务同事");
    expect(out.missing).toEqual(["文风"]);
    expect(out.placeholders).toEqual(["读者", "文风"]);
  });

  it("treats a whitespace-only value as not filled", () => {
    const out = interpolateTemplate("{{文风}}", { 文风: "   " });
    expect(out.text).toBe("{{文风}}");
    expect(out.missing).toEqual(["文风"]);
  });

  it("trims the pasted value without eating its inner newlines", () => {
    const out = interpolateTemplate("```\n${代码}\n```", { 代码: "\n  a\n  b\n  " });
    expect(out.text).toBe("```\na\n  b\n```");
  });

  it("ignores values for names the template never uses", () => {
    const out = interpolateTemplate("无占位符", { 多余: "值" });
    expect(out.text).toBe("无占位符");
    expect(out.missing).toEqual([]);
  });
});

describe("searchFilter", () => {
  const rows: PromptTemplate[] = [
    template({
      id: "a",
      user_id: null,
      title: "中文润色（不改原意）",
      content: "文风：{{文风}}",
      description: "发稿前过一遍",
      tags: ["写作", "中文"],
      category: "写作",
    }),
    template({
      id: "b",
      title: "Code Review",
      content: "请审查下面的 {{语言}} 代码",
      tags: ["编程"],
      category: "编程",
    }),
    template({ id: "c", title: "会议纪要", content: "整理要点", category: "办公" }),
  ];
  const ids = (filter: Parameters<typeof searchFilter>[1]) =>
    searchFilter(rows, filter).map((r) => r.id);

  it("matches Chinese substrings across title, content, description and tags", () => {
    expect(ids({ q: "润色" })).toEqual(["a"]);
    expect(ids({ q: "下面的" })).toEqual(["b"]);
    expect(ids({ q: "发稿" })).toEqual(["a"]);
    expect(ids({ q: "编程" })).toEqual(["b"]);
  });

  it("is case-insensitive for latin text", () => {
    expect(ids({ q: "code review" })).toEqual(["b"]);
    expect(ids({ q: "CODE" })).toEqual(["b"]);
  });

  it("ignores stray spaces, which Chinese input produces constantly", () => {
    expect(ids({ q: "中文 润色" })).toEqual(["a"]);
    expect(ids({ q: "code  review" })).toEqual(["b"]);
  });

  it("requires every term to hit (AND, not OR)", () => {
    expect(ids({ q: "润色 编程" })).toEqual([]);
    expect(ids({ q: "代码 review" })).toEqual(["b"]);
  });

  it("splits by scope on ownership, not on a flag", () => {
    expect(ids({ scope: "preset" })).toEqual(["a"]);
    expect(ids({ scope: "mine" })).toEqual(["b", "c"]);
    expect(ids({ scope: "all" })).toEqual(["a", "b", "c"]);
    expect(ids({})).toEqual(["a", "b", "c"]);
  });

  it("filters by category exactly, blank meaning no filter", () => {
    expect(ids({ category: "写作" })).toEqual(["a"]);
    expect(ids({ category: " 写作 " })).toEqual(["a"]);
    expect(ids({ category: "" })).toEqual(["a", "b", "c"]);
    expect(ids({ category: "不存在" })).toEqual([]);
  });

  it("does not mutate the input array", () => {
    const before = rows.map((r) => r.id);
    searchFilter(rows, { q: "会议" });
    expect(rows.map((r) => r.id)).toEqual(before);
  });
});

describe("sortPromptGroups", () => {
  it("puts presets first by sort_order, then my templates by updated_at desc", () => {
    const sorted = sortPromptGroups([
      template({ id: "mine-old", updated_at: "2026-01-01T00:00:00Z" }),
      PRESET("p20", 20),
      template({ id: "mine-new", updated_at: "2026-09-09T00:00:00Z" }),
      PRESET("p10", 10),
    ]).map((r) => r.id);
    expect(sorted).toEqual(["p10", "p20", "mine-new", "mine-old"]);
  });

  it("breaks ties deterministically so the list never reshuffles itself", () => {
    const same = template({ id: "x", updated_at: "2026-05-05T00:00:00Z" });
    const sameLater = template({
      id: "y",
      updated_at: "2026-05-05T00:00:00Z",
      created_at: "2026-01-01T00:00:00Z",
    });
    const mine = sortPromptGroups([sameLater, same]).map((r) => r.id);
    expect(mine).toEqual(["x", "y"]);
    const presets = sortPromptGroups([
      PRESET("b", 10),
      template({ id: "a", user_id: null, sort_order: 10 }),
    ]).map((r) => r.id);
    expect(presets).toEqual(["a", "b"]);
  });

  it("survives unparseable timestamps instead of scattering the list", () => {
    const ids = sortPromptGroups([
      template({ id: "broken", updated_at: "not-a-date" }),
      template({ id: "good", updated_at: "2026-03-03T00:00:00Z" }),
    ]).map((r) => r.id);
    expect(ids).toEqual(["good", "broken"]);
  });

  it("returns a new array", () => {
    const rows = [PRESET("p2", 2), PRESET("p1", 1)];
    expect(sortPromptGroups(rows)).not.toBe(rows);
    expect(rows.map((r) => r.id)).toEqual(["p2", "p1"]);
  });

  it("reads ownership off user_id", () => {
    expect(isPresetPrompt(PRESET("p", 1))).toBe(true);
    expect(isPresetPrompt(template({ id: "m" }))).toBe(false);
  });
});

describe("parseTags", () => {
  it("splits on the separators people actually type, deduped and cleaned", () => {
    expect(parseTags("编程、测试,  单元测试；测试， ")).toEqual([
      "编程",
      "测试",
      "单元测试",
    ]);
    expect(parseTags("  ")).toEqual([]);
  });
});

describe("validatePromptForm", () => {
  it("accepts a minimal form and defaults nothing away", () => {
    expect(
      validatePromptForm(form({ title: "周报", content: "本周做了什么" }))
    ).toEqual({});
  });

  it("demands title and content", () => {
    const errors = validatePromptForm(form());
    expect(errors.title).toContain("标题");
    expect(errors.content).toContain("内容");
    // 空分类拦不住是因为表单默认就是服务端的默认值；真清空了才报错。
    expect(validatePromptForm(form({ title: "t", content: "c" }))).toEqual({});
    expect(
      validatePromptForm(form({ title: "t", content: "c", category: "  " })).category
    ).toContain("分类");
  });

  it("states the server-side ceilings", () => {
    const errors = validatePromptForm(
      form({
        title: "长".repeat(PROMPT_LIMITS.title.max + 1),
        content: "长".repeat(PROMPT_LIMITS.content.max + 1),
        category: "长".repeat(PROMPT_LIMITS.category.max + 1),
        description: "长".repeat(PROMPT_LIMITS.description.max + 1),
        tags: Array.from({ length: PROMPT_LIMITS.tags.maxCount + 1 }, (_, i) => `t${i}`).join("、"),
      })
    );
    expect(errors.title).toContain(String(PROMPT_LIMITS.title.max));
    expect(errors.content).toContain(String(PROMPT_LIMITS.content.max));
    expect(errors.category).toContain(String(PROMPT_LIMITS.category.max));
    expect(errors.description).toContain(String(PROMPT_LIMITS.description.max));
    expect(errors.tags).toContain(String(PROMPT_LIMITS.tags.maxCount));
  });

  it("catches one over-long tag rather than the whole list", () => {
    const errors = validatePromptForm(
      form({ tags: `正常、${"长".repeat(PROMPT_LIMITS.tags.maxLen + 1)}` })
    );
    expect(errors.tags).toContain(String(PROMPT_LIMITS.tags.maxLen));
  });
});

describe("promptCreateBody / promptPatchBody", () => {
  const original = template({
    id: "p",
    title: "周报",
    content: "本周：",
    category: "办公",
    tags: ["写作"],
    description: "给主管看",
  });

  it("sends the whole object on create, blank description as null", () => {
    expect(
      promptCreateBody(
        form({
          title: " 翻译 ",
          content: " 保留格式 ",
          category: "  ",
          tags: "翻译、 术语",
        })
      )
    ).toEqual({
      title: "翻译",
      content: " 保留格式 ",
      category: DEFAULT_PROMPT_CATEGORY,
      tags: ["翻译", "术语"],
      description: null,
    });
  });

  it("loads an existing template back into the form", () => {
    expect(promptFormFromTemplate(original)).toEqual({
      title: "周报",
      content: "本周：",
      category: "办公",
      description: "给主管看",
      tags: "写作",
    });
    expect(promptFormFromTemplate(null)).toEqual(EMPTY_PROMPT_FORM);
  });

  it("sends nothing when the form still matches the server", () => {
    expect(promptPatchBody(promptFormFromTemplate(original), original)).toBeNull();
  });

  it("sends only the touched fields", () => {
    const next = { ...promptFormFromTemplate(original), title: "月报" };
    expect(promptPatchBody(next, original)).toEqual({ title: "月报" });
  });

  it("treats a cleared description as an explicit null and reordered tags as a change", () => {
    const next = { ...promptFormFromTemplate(original), description: "", tags: "写作、模板" };
    expect(promptPatchBody(next, original)).toEqual({
      description: null,
      tags: ["写作", "模板"],
    });
  });
});
