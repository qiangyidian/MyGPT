// 全项目唯一的 CSV 出口：BOM、单元格转义、表头拼装都在这一处。
//
// 少了 BOM，Excel 会按本地代码页把中文表头读成乱码；少了引号转义，一个含逗号
// 或换行的字段会让整行串列 —— 两者都是「导出看着成功、打开才发现错」的坑。
import { saveBlobToDisk } from "@/lib/download";

/** UTF-8 BOM：Excel 打开中文 CSV 的必要条件（与 `redeem-batch.ts` 原来那个同源）。 */
export const CSV_BOM = "﻿";

export type CsvValue = string | number | boolean | null | undefined;

/**
 * CSV 注入（formula injection）：以 ``=`` ``+`` ``-`` ``@`` 或制表符开头的单元格，
 * Excel / WPS / Google Sheets 打开时会**当公式求值**，于是 ``=HYPERLINK(...)`` 之类
 * 能把读者点出去的一个链接变成一次带凭证的外联。导出的每一列都可能是用户输入 ——
 * 审计日志里的操作人邮箱与目标 id、兑换码批次名、用量报表里的用户标签 —— 而"导出
 * 给人看"正是这条链路的终点，所以必须在这里挡，而不是指望调用方记得筛。
 *
 * 只在值**是字符串**时加前缀单引号：数字（成本、token 数）以 ``-`` 开头是负数，加
 * 引号会把 ``-0.5`` 变成文本，报表里那一列就不再能求和了。
 */
const FORMULA_LEAD = /^(?:[=+@\t\r]|-(?![=]))/;

export function csvSafeValue(value: CsvValue): string {
  if (value === null || value === undefined) return "";
  if (typeof value !== "string") return String(value);
  return FORMULA_LEAD.test(value) ? `'${value}` : value;
}

export function csvCell(value: CsvValue): string {
  const text = csvSafeValue(value);
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function buildCsv(
  headers: readonly string[],
  rows: readonly (readonly CsvValue[])[]
): string {
  const lines = [headers.map(csvCell).join(",")];
  for (const row of rows) lines.push(row.map(csvCell).join(","));
  return `${CSV_BOM}${lines.join("\n")}`;
}

/** 拼一份 CSV 并立即触发浏览器下载（运营导出的唯一落地方式）。 */
export function downloadCsvFile(
  filename: string,
  headers: readonly string[],
  rows: readonly (readonly CsvValue[])[]
): void {
  const blob = new Blob([buildCsv(headers, rows)], { type: "text/csv;charset=utf-8" });
  saveBlobToDisk(blob, filename);
}
