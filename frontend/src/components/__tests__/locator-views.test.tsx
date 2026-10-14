import { beforeAll, describe, expect, it } from "vitest";
import { renderToString } from "react-dom/server";
import * as React from "react";

import { HighlightOverlay } from "@/components/context/locators/highlight-overlay";
import { SheetRangeLocatorView } from "@/components/context/locators/sheet-range-locator";
import { SlideLocatorView } from "@/components/context/locators/slide-locator";
import { TextRangeLocatorView } from "@/components/context/locators/text-range-locator";

beforeAll(() => {
  (globalThis as { React?: unknown }).React = React;
});

const countOf = (html: string, needle: string) => html.split(needle).length - 1;

describe("HighlightOverlay", () => {
  it("每个框渲染一层，绝不合并", () => {
    const html = renderToString(
      <HighlightOverlay
        boxes={[
          [0.1, 0.2, 0.4, 0.25],
          [0.1, 0.26, 0.4, 0.31],
        ]}
      />
    );
    expect(countOf(html, "data-highlight-box")).toBe(2);
  });

  it("把归一化比例原样写成 CSS 百分比", () => {
    const html = renderToString(<HighlightOverlay boxes={[[0.25, 0.5, 0.75, 0.6]]} />);
    expect(html).toContain("left:25%");
    expect(html).toContain("top:50%");
    expect(html).toContain("width:50%");
    expect(html).toContain("height:10%");
  });

  it("没有框时什么都不渲染", () => {
    expect(renderToString(<HighlightOverlay boxes={[]} />)).toBe("");
  });
});

describe("TextRangeLocatorView", () => {
  const loc = {
    kind: "text_range" as const,
    text: "前文命中内容后文",
    start: 2,
    end: 6,
  };

  it("命中片段落在 <mark> 里，且原文一字不差", () => {
    const html = renderToString(<TextRangeLocatorView loc={loc} />);
    expect(html).toContain("<mark");
    expect(html).toContain(">命中内容</mark>");
    expect(html).toContain("前文");
    expect(html).toContain("后文");
  });

  it("偏移越界时不丢字符、不抛错", () => {
    const html = renderToString(
      <TextRangeLocatorView loc={{ kind: "text_range", text: "短", start: 99, end: 120 }} />
    );
    expect(html).toContain("短");
  });
});

describe("SheetRangeLocatorView", () => {
  const loc = {
    kind: "sheet_range" as const,
    sheet: "Sheet1",
    header: ["序号", "条款"],
    rows: [
      ["1", "8.5.1"],
      ["2", "8.5.2"],
      ["3", "8.5.3"],
    ],
    highlight: [1, 3] as [number, number],
  };

  it("只标记命中行", () => {
    const html = renderToString(<SheetRangeLocatorView loc={loc} />);
    expect(countOf(html, "data-row-hit")).toBe(2);
    expect(html).toContain("8.5.2");
    expect(html).toContain("8.5.3");
  });

  it("渲染全部行与表头", () => {
    const html = renderToString(<SheetRangeLocatorView loc={loc} />);
    expect(countOf(html, "<tr")).toBe(4); // 1 表头 + 3 数据行
    expect(countOf(html, "<th ")).toBe(2); // 注意 "<thead" 也以 "<th" 开头
  });
});

describe("SlideLocatorView", () => {
  const loc = {
    kind: "slide" as const,
    slide_no: 3,
    width: 960,
    height: 540,
    shapes: [
      { rect: [10, 10, 400, 60] as [number, number, number, number], text: "标题", hit: false },
      {
        rect: [10, 100, 400, 60] as [number, number, number, number],
        text: "命中正文",
        hit: true,
      },
    ],
  };

  it("只有命中的形状带标记", () => {
    const html = renderToString(<SlideLocatorView loc={loc} />);
    expect(countOf(html, "data-shape-hit")).toBe(1);
  });

  it("用 viewBox 表达幻灯片尺寸（缩放交给 SVG，不需要归一化）", () => {
    const html = renderToString(<SlideLocatorView loc={loc} />);
    expect(html).toContain('viewBox="0 0 960 540"');
  });

  it("形状文本照常展示", () => {
    const html = renderToString(<SlideLocatorView loc={loc} />);
    expect(html).toContain("标题");
    expect(html).toContain("命中正文");
  });
});
