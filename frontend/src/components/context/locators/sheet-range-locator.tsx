"use client";

import { highlightedRows, type SheetRangeLocator } from "@/lib/locator";

/** 表格的命中行高亮。行区间由后端按 chunk 的字符区间与逐行偏移算出。 */
export function SheetRangeLocatorView({ loc }: { loc: SheetRangeLocator }) {
  const hits = new Set(highlightedRows(loc));
  return (
    <div className="space-y-2">
      <p className="text-[11px] text-muted-foreground">工作表：{loc.sheet}</p>
      <div className="overflow-auto rounded-lg border border-border">
        <table className="w-full border-collapse text-xs">
          <thead>
            <tr className="bg-muted/50">
              {loc.header.map((cell, i) => (
                <th
                  key={i}
                  className="border-b border-border px-2 py-1.5 text-left font-medium"
                >
                  {cell}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loc.rows.map((row, i) => (
              <tr
                key={i}
                {...(hits.has(i) ? { "data-row-hit": "" } : {})}
                className={hits.has(i) ? "bg-amber-300/40" : undefined}
              >
                {row.map((cell, j) => (
                  <td key={j} className="border-b border-border/50 px-2 py-1 align-top">
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
