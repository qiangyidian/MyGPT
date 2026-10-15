// 提示词库剩在 lib/prompt-library 里的纯函数。占位符插值本身在
// prompt-apply.test.ts 里钉过了，这里管的是这一侧特有的口径：占位符怎么认、
// 下标怎么夹紧。原先「插进输入框光标处」「从草稿反推表单初值」两组已随输入框
// 工具栏的提示词库入口一并删除（模板只在设置页管理），对应断言同步移除。
import { describe, expect, it } from "vitest";

import { PROMPT_LIMITS } from "@/lib/prompt-apply";
import {
  copyTitle,
  duplicateAsOwnInput,
  extractVariables,
  formatVariable,
  insertAtCaret,
  previewText,
} from "@/lib/prompt-library";
import type { PromptTemplate } from "@/lib/types";

function template(overrides: Partial<PromptTemplate> = {}): PromptTemplate {
  return {
    id: "t-1",
    user_id: "user-1",
    title: "周报整理",
    content: "正文",
    category: "办公",
    tags: ["汇报"],
    description: "说明",
    sort_order: 0,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    ...overrides,
  };
}

describe("extractVariables", () => {
  it("两种写法都认，并按首次出现顺序去重", () => {
    const found = extractVariables("用{{语气}}改写 ${字数} 字，{{ 语气 }} 再来一次 ${别的}");
    expect(found.map((v) => v.name)).toEqual(["语气", "字数", "别的"]);
    expect(found[0]).toMatchObject({ raw: "{{语气}}", syntax: "curly", index: 1 });
    expect(found[1]).toMatchObject({ raw: "${字数}", syntax: "dollar" });
  });

  it("同名不同写法只留一个格子", () => {
    expect(extractVariables("${字数} 与 {{字数}}").map((v) => v.raw)).toEqual(["${字数}"]);
  });

  it("变量名可以含中文、空格、连字符与数字", () => {
    expect(extractVariables("{{ 目标 语言 }} ${A-1}").map((v) => v.name)).toEqual([
      "目标 语言",
      "A-1",
    ]);
  });

  it("空壳、跨行、超长、单括号、嵌套都不算变量", () => {
    const noise = [
      "{{}}",
      "{{   }}",
      "${}",
      "{{多\n行}}",
      `{{${"x".repeat(41)}}}`,
      "单括号 {a}",
      "嵌套 {{a{b}c}}",
    ].join("\n");
    expect(extractVariables(noise)).toEqual([]);
  });
});

describe("formatVariable", () => {
  it("按写法还原原文", () => {
    expect(formatVariable("语气")).toBe("{{语气}}");
    expect(formatVariable("字数", "dollar")).toBe("${字数}");
  });
});

describe("insertAtCaret", () => {
  it("纯插入落在光标处，光标移到插入内容之后", () => {
    expect(insertAtCaret("abcd", 2, 2, "XY")).toEqual({ value: "abXYcd", caret: 4 });
  });

  it("有选区时替换选中的文字", () => {
    expect(insertAtCaret("abcd", 1, 3, "Z")).toEqual({ value: "aZd", caret: 2 });
  });

  it("越界与反向的下标先夹紧，不抛出半截字符串", () => {
    expect(insertAtCaret("ab", 99, 99, "X")).toEqual({ value: "abX", caret: 3 });
    expect(insertAtCaret("ab", -5, -1, "X")).toEqual({ value: "Xab", caret: 1 });
    // NaN（textarea 偶尔会给出）退化成「接在末尾」而不是变成 0。
    expect(insertAtCaret("ab", Number.NaN, Number.NaN, "X")).toEqual({
      value: "abX",
      caret: 3,
    });
    // start > end 不成立时按纯插入处理。
    expect(insertAtCaret("abcd", 3, 1, "X")).toEqual({ value: "abcXd", caret: 4 });
  });
});

describe("previewText", () => {
  it("折叠空白，超长时按上限截断并补省略号", () => {
    expect(previewText("a\n\n  b   c")).toBe("a b c");
    expect(previewText("一二三四五", 3)).toBe("一二…");
    const long = "正".repeat(120);
    const cut = previewText(long);
    expect(cut.length).toBe(80);
    expect(cut.endsWith("…")).toBe(true);
  });
});

describe("副本标题与另存为", () => {
  it("加后缀，空标题有兜底", () => {
    expect(copyTitle("周报")).toBe("周报（副本）");
    expect(copyTitle("   ")).toBe("未命名模板（副本）");
  });

  it("先按上限截断再接后缀，长度绝不越界", () => {
    const copy = copyTitle("长".repeat(200));
    expect(copy.length).toBe(PROMPT_LIMITS.title.max);
    expect(copy.endsWith("（副本）")).toBe(true);
  });

  it("另存为自己的模板时内容原样带走，标题加后缀", () => {
    const source = template({ id: "p-1", user_id: null, title: "代码审查", content: "看 ${代码}" });
    const body = duplicateAsOwnInput(source);
    expect(body).toEqual({
      title: "代码审查（副本）",
      content: "看 ${代码}",
      category: "办公",
      tags: ["汇报"],
      description: "说明",
    });
    // 标签是新数组：改副本不该动到源模板。
    expect(body.tags).not.toBe(source.tags);
  });
});
