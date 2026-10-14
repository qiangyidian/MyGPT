"use client";

import { toPercentBox } from "@/lib/locator";

/**
 * 归一化高亮框层。**一行一个框，绝不合并** —— 把多行并成一个矩形就等于把
 * 相邻 chunk 的内容也框进去，那正是这个功能要避免的谎言。
 *
 * 单独成一个组件有两个原因：它是唯一需要独立测的渲染逻辑（纯 props 进、
 * 纯 DOM 出），而且它并不关心底图是从哪来的。
 */
export function HighlightOverlay({ boxes }: { boxes: number[][] }) {
  if (!boxes.length) return null;
  return (
    <>
      {boxes.map((box, i) => (
        <span
          key={i}
          data-highlight-box
          className="pointer-events-none absolute bg-amber-300/40 ring-1 ring-amber-500/60"
          style={toPercentBox(box)}
        />
      ))}
    </>
  );
}
