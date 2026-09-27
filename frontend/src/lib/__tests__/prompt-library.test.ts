// 提示词库「放进输入框」这一侧的纯函数。占位符插值本身在 prompt-apply.test.ts 里
// 钉过了，这里钉的是另一半：插在哪、光标落在哪、什么时候补空行 —— 这些错了不会报错，
// 只会让用户每次插入都要手动挪光标，所以必须用断言代替肉眼验收。
import { describe, expect, it } from "vitest";

import { EMPTY_PROMPT_FORM, PROMPT_LIMITS, validatePromptForm } from "@/lib/prompt-apply";
import {
  copyTitle,
  duplicateAsOwnInput,
  extractVariables,
  formatVariable,
  insertAtCaret,
  insertTemplate,
  previewText,
  promptFormFromDraft,
  titleFromDraft,
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

describe("insertTemplate", () => {
  it("空输入框：整段落下，光标选中第一个占位符", () => {
    const result = insertTemplate("", "请用{{语气}}改写：");
    expect(result.value).toBe("请用{{语气}}改写：");
    expect(result.selection).toEqual({ start: 2, end: 8 });
    expect(result.value.slice(2, 8)).toBe("{{语气}}");
    expect(result.caret).toBe(2);
  });

  it("没有占位符时把光标放在模板末尾", () => {
    const result = insertTemplate("", "纯文本");
    expect(result).toEqual({ value: "纯文本", caret: 3, selection: null });
  });

  it("替换选区，并且只在会挤在一起时补空行", () => {
    const result = insertTemplate("AAA-BBB-CCC", "T{{x}}", { start: 4, end: 7 });
    expect(result.value).toBe("AAA-\n\nT{{x}}\n\n-CCC");
    expect(result.value.slice(7, 12)).toBe("{{x}}");
    expect(result.selection).toEqual({ start: 7, end: 12 });
  });

  it("前面已经是行尾时不再补空行（连着插不会长出一串空行）", () => {
    const result = insertTemplate("第一行\n", "{{a}}", { start: 4, end: 4 });
    expect(result.value).toBe("第一行\n{{a}}");
    expect(result.value.slice(4, 9)).toBe("{{a}}");
    expect(result.selection).toEqual({ start: 4, end: 9 });
  });

  it("插在行首时后面的正文换行保持原样", () => {
    const result = insertTemplate("帮我写周报", "{{语气}}", { start: 0, end: 0 });
    expect(result.value).toBe("{{语气}}\n\n帮我写周报");
    expect(result.value.slice(0, 6)).toBe("{{语气}}");
    expect(result.selection).toEqual({ start: 0, end: 6 });
  });

  it("下标越界只夹紧到末尾，不会切出残缺文本", () => {
    const result = insertTemplate("abc", "X", { start: 99, end: 99 });
    // 夹紧成「接在末尾」之后，规则照旧：前面不是行尾就补一个空行。
    expect(result).toEqual({ value: "abc\n\nX", caret: 6, selection: null });
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

describe("titleFromDraft", () => {
  it("取第一个非空行，并去掉开头的标记符号", () => {
    expect(titleFromDraft("\n\n# 标题行\n正文")).toBe("标题行");
    expect(titleFromDraft("- 列表开头")).toBe("列表开头");
    expect(titleFromDraft("   \n  ")).toBe("");
  });

  it("超出上限时截断并补省略号", () => {
    expect(titleFromDraft("标题", 1)).toBe("…");
    expect(titleFromDraft("标".repeat(200), 10).length).toBe(10);
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

describe("promptFormFromDraft", () => {
  it("正文就是草稿原文，标题按首行猜，其余落在默认表单上", () => {
    expect(promptFormFromDraft("整理这份周报\n细节")).toEqual({
      ...EMPTY_PROMPT_FORM,
      title: "整理这份周报",
      content: "整理这份周报\n细节",
    });
  });

  it("存草稿生成的表单能直接通过校验（否则弹窗会亮一个红叉）", () => {
    expect(validatePromptForm(promptFormFromDraft("整理这份周报"))).toEqual({});
  });
});
