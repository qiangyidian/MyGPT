"use client";

import { lazy, memo, Suspense } from "react";

import { cn } from "@/lib/utils";

/** 渲染器本体单独成块：react-markdown + remark-gfm + rehype-highlight（连带
 *  highlight.js）是首屏最重的一笔依赖，而它只在真的要有内容被渲染时才用得上。 */
const LazyMarkdownBody = lazy(() =>
  import("@/components/markdown-body").then((m) => ({ default: m.MarkdownBody }))
);

/** 分块到达之前先把正文当纯文本摊出来：内容可读、盒子和最终渲染接近，不会等加载
 *  完再跳一次版。 */
function PlainTextPreview({
  content,
  className,
}: {
  content: string;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "max-w-none break-words whitespace-pre-wrap text-sm leading-relaxed text-foreground",
        className
      )}
    >
      {content}
    </div>
  );
}

/**
 * Markdown renderer with GFM, syntax highlighting, and copy buttons on code
 * blocks. Props are unchanged from the pre-split version; the only difference
 * is that the heavy renderer is a lazy chunk behind a plain-text placeholder.
 */
export const Markdown = memo(function Markdown({
  content,
  className,
  lite = false,
}: {
  content: string;
  className?: string;
  /** Skip syntax highlighting while a message streams (see ``MarkdownBody``). */
  lite?: boolean;
}) {
  return (
    <Suspense fallback={<PlainTextPreview content={content} className={className} />}>
      <LazyMarkdownBody content={content} className={className} lite={lite} />
    </Suspense>
  );
});
