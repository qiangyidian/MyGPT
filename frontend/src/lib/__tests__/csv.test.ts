// CSV 是运营唯一的存档出口：转义写错不会报错，只会导出一张串列的表，
// 所以这些边界必须在 lib 层钉死（vitest 是 `environment: "node"`）。
import { describe, expect, it } from "vitest";

import { buildCsv, CSV_BOM, csvCell, csvSafeValue } from "@/lib/csv";

// 用码点构造，避免「换行到底是 CRLF 还是 LF」取决于测试文件自己的行尾。
const LF = String.fromCharCode(10);
const CR = String.fromCharCode(13);

describe("csvCell", () => {
  it("逗号、引号、换行都要包起来，且引号双写", () => {
    expect(csvCell("普通文本")).toBe("普通文本");
    expect(csvCell('we"ird')).toBe('"we""ird"');
    expect(csvCell("we,ird")).toBe('"we,ird"');
    expect(csvCell(`we${LF}ird`)).toBe(`"we${LF}ird"`);
    expect(csvCell(`we${CR}ird`)).toBe(`"we${CR}ird"`);
  });

  it("null / undefined 导出空单元格，而不是字面量 null", () => {
    expect(csvCell(null)).toBe("");
    expect(csvCell(undefined)).toBe("");
    expect(csvCell(0)).toBe("0");
    expect(csvCell(false)).toBe("false");
  });
});

describe("buildCsv", () => {
  it("以 BOM 开头（Excel 认中文表头的前提），表头在第一行", () => {
    const csv = buildCsv(["日期", "消息数"], [["2026-09-01", 3]]);
    expect(csv.startsWith(CSV_BOM)).toBe(true);
    expect(csv.slice(CSV_BOM.length).split(LF)).toEqual(["日期,消息数", "2026-09-01,3"]);
  });

  it("空表只有一行表头，不会多出空行", () => {
    expect(buildCsv(["时间", "操作"], [])).toBe(`${CSV_BOM}时间,操作`);
  });

  it("含分隔符的值不会把后面的列挤走", () => {
    const csv = buildCsv(["目标", "详情"], [['we,ird "x"', "第二列"]]);
    expect(csv).toBe(`${CSV_BOM}目标,详情${LF}"we,ird ""x""",第二列`);
  });
});

describe("csvSafeValue（CSV 公式注入）", () => {
  // 审计导出的「操作人邮箱」、兑换码导出的「批次名」、用量导出的「用户标签」都是用户
  // 可控字符串。电子表格会把 = / + / - / @ 开头的单元格当公式求值，所以引号转义之外
  // 还得挡这一步 —— 而且必须在 csv.ts 这一处挡，三个导出面板共用同一个出口。
  it("以公式前缀开头的字符串被降级成文本", () => {
    for (const lead of ["=", "+", "-", "@"]) {
      expect(csvSafeValue(`${lead}HYPERLINK("http://evil","点我")`)).toBe(
        `'${lead}HYPERLINK("http://evil","点我")`
      );
    }
    expect(csvSafeValue("\t=cmd")).toBe("'\t=cmd");
  });

  it("正常内容一字不改（引号只加在最前面，不参与正文）", () => {
    expect(csvSafeValue("admin@mychat")).toBe("admin@mychat");
    expect(csvSafeValue("2026-09-01")).toBe("2026-09-01");
    expect(csvSafeValue("-= 减号在中间不算公式")).toBe("-= 减号在中间不算公式");
    expect(csvSafeValue(null)).toBe("");
    expect(csvSafeValue(undefined)).toBe("");
  });

  it("数字负号不能加引号：那是负数，加了这一列就没法再求和", () => {
    // 用量报表的成本列、审计里的余额差额都会以数字传进来；只有字符串才降级。
    expect(csvSafeValue(-0.5)).toBe("-0.5");
    expect(csvCell(-0.5)).toBe("-0.5");
  });

  it("注入防护穿过 buildCsv 生效（导出层没有第二条路径）", () => {
    const csv = buildCsv(["操作人", "成本"], [['=cmd|"/c calc"', -1.5]]);
    expect(csv.slice(CSV_BOM.length)).toBe("操作人,成本" + LF + '"\'=cmd|""/c calc""",-1.5');
  });
});
