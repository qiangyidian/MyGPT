import { describe, expect, it } from "vitest";

import {
  highlightedRows,
  splitTextRange,
  toPercentBox,
  type SheetRangeLocator,
  type TextRangeLocator,
} from "@/lib/locator";

describe("toPercentBox", () => {
  it("把归一化比例换算成 CSS 百分比", () => {
    expect(toPercentBox([0.25, 0.5, 0.75, 0.6])).toEqual({
      left: "25%",
      top: "50%",
      width: "50%",
      height: "10%",
    });
  });

  it("夹住越界比例 —— 越界会画出跑到页外的框", () => {
    expect(toPercentBox([-0.2, -1, 1.4, 2])).toEqual({
      left: "0%",
      top: "0%",
      width: "100%",
      height: "100%",
    });
  });

  it("不产生浮点尾巴（0.5→0.6 是 0.099999...，直接乘 100 会得到 9.999999999999998%）", () => {
    expect(toPercentBox([0.5, 0.5, 0.8, 0.6]).height).toBe("10%");
  });

  it("长度不足 4 个值时给零框而不是崩", () => {
    expect(toPercentBox([])).toEqual({
      left: "0%",
      top: "0%",
      width: "0%",
      height: "0%",
    });
  });
});

describe("highlightedRows", () => {
  it("按 [起, 止) 展开行号", () => {
    const loc = {
      kind: "sheet_range",
      rows: [["a"], ["b"], ["c"], ["d"], ["e"]],
      highlight: [1, 4],
    } as unknown as SheetRangeLocator;
    expect(highlightedRows(loc)).toEqual([1, 2, 3]);
  });

  it("highlight 缺失时返回空数组而不是崩", () => {
    const loc = {
      kind: "sheet_range",
      rows: [["a"]],
      highlight: null,
    } as unknown as SheetRangeLocator;
    expect(highlightedRows(loc)).toEqual([]);
  });

  it("止超出实际行数时按行数截断", () => {
    const loc = {
      kind: "sheet_range",
      rows: [["a"], ["b"]],
      highlight: [0, 99],
    } as unknown as SheetRangeLocator;
    expect(highlightedRows(loc)).toEqual([0, 1]);
  });
});

describe("splitTextRange", () => {
  it("切成 前 / 命中 / 后 三段，三段拼回去等于原文", () => {
    const loc: TextRangeLocator = {
      kind: "text_range",
      text: "前面的话命中内容后面的话",
      start: 4,
      end: 8,
    };
    const [before, hit, after] = splitTextRange(loc);
    expect(hit).toBe("命中内容");
    expect(before + hit + after).toBe(loc.text);
  });

  it("偏移越界时截断而不丢字符", () => {
    const loc: TextRangeLocator = {
      kind: "text_range",
      text: "短文本",
      start: -5,
      end: 99,
    };
    const [before, hit, after] = splitTextRange(loc);
    expect(before).toBe("");
    expect(hit).toBe("短文本");
    expect(after).toBe("");
  });

  it("起 > 止 时退化为空命中，而不是反过来切", () => {
    const loc: TextRangeLocator = {
      kind: "text_range",
      text: "abcdef",
      start: 4,
      end: 2,
    };
    const [before, hit, after] = splitTextRange(loc);
    expect(before + hit + after).toBe("abcdef");
    expect(hit).toBe("");
  });
});
