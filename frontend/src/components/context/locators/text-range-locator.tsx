"use client";

import { splitTextRange, type TextRangeLocator } from "@/lib/locator";

/**
 * 兜底层：把原文窗口里的命中片段标出来。
 *
 * 这一层对**任何有文本的格式**都成立 —— Word、RTF、纯文本、HTML、EPUB 全部走它。
 * 它不是"降级凑数"：对没有分页概念的格式，「原文的哪一段」就是唯一诚实的答案。
 */
export function TextRangeLocatorView({ loc }: { loc: TextRangeLocator }) {
  const [before, hit, after] = splitTextRange(loc);
  return (
    <div className="space-y-2">
      <p className="text-[11px] text-muted-foreground">
        原文片段（该格式不含版面坐标，无法定位到原页位置）
      </p>
      <pre className="whitespace-pre-wrap break-words rounded-lg border border-border bg-muted/30 p-3 font-sans text-xs leading-relaxed">
        {before}
        <mark className="bg-amber-300/60 px-0.5">{hit}</mark>
        {after}
      </pre>
    </div>
  );
}
