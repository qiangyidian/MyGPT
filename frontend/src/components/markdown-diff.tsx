"use client";

import { DIFF_DEFAULT_CONTEXT, hunks, type DiffRow, type UnifiedDiff } from "@/lib/diff";
import { cn } from "@/lib/utils";

/**
 * 统一补丁的着色视图。行号、折叠位置全部由 `@/lib/diff` 算出来，这里只画——
 * 跟着 `markdown-body` 一起被 lazy 加载，不进首屏。
 */

const ROW_CLASS: Record<DiffRow["kind"], string> = {
  equal: "text-[#e6e6e6]",
  add: "bg-emerald-500/15 text-emerald-100",
  del: "bg-red-500/15 text-red-100",
};

const ROW_SIGN: Record<DiffRow["kind"], string> = {
  equal: " ",
  add: "+",
  del: "-",
};

const GUTTER =
  "w-9 shrink-0 select-none text-right font-mono text-[11px] leading-6 tabular-nums text-zinc-500";

function DiffLine({ row }: { row: DiffRow }) {
  return (
    <div className={cn("flex w-max min-w-full gap-2 px-3", ROW_CLASS[row.kind])}>
      <span className={GUTTER}>{row.oldNumber ?? ""}</span>
      <span className={GUTTER}>{row.newNumber ?? ""}</span>
      <span className="whitespace-pre font-mono text-[13px] leading-6">
        {`${ROW_SIGN[row.kind]}${row.text}`}
      </span>
    </div>
  );
}

/** 补丁顶部的文件名：多文件只报数量，别用一个文件的路去糊另一文件的行号。 */
export function diffFileLabel(patch: UnifiedDiff): string | null {
  if (patch.fileCount > 1) return `${patch.fileCount} 个文件`;
  return patch.newPath ?? patch.oldPath ?? null;
}

export function MarkdownDiff({
  rows,
  fileLabel,
}: {
  rows: DiffRow[];
  fileLabel?: string | null;
}) {
  const blocks = hunks(rows, DIFF_DEFAULT_CONTEXT);
  const added = rows.reduce((n, r) => n + (r.kind === "add" ? 1 : 0), 0);
  const removed = rows.reduce((n, r) => n + (r.kind === "del" ? 1 : 0), 0);

  return (
    <div className="overflow-x-auto py-1">
      <div className="px-3 pb-1 font-mono text-[11px] text-zinc-400">
        {fileLabel ? `${fileLabel} · ` : ""}新增 {added} 行 · 删除 {removed} 行
      </div>
      {blocks.map((block, index) =>
        block.kind === "fold" ? (
          <div
            key={`fold-${index}`}
            className="my-1 border-y border-white/5 bg-white/[0.02] px-3 py-1 font-mono text-[11px] text-zinc-500"
          >
            未变更的 {block.count} 行已折叠（自第 {block.oldStart} 行起）
          </div>
        ) : (
          <div key={`rows-${index}`}>
            {block.rows.map((row, rowIndex) => (
              <DiffLine key={`${row.kind}-${row.oldNumber ?? 0}-${row.newNumber ?? 0}-${rowIndex}`} row={row} />
            ))}
          </div>
        )
      )}
    </div>
  );
}
