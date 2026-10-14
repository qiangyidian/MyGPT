"use client";

import type { SlideLocator } from "@/lib/locator";

/**
 * 幻灯片形状高亮。坐标是 pt，由 `viewBox` 负责等比缩放到容器宽度 ——
 * 所以这里**不需要**归一化，也不受抽屉宽度变化影响。
 */
export function SlideLocatorView({ loc }: { loc: SlideLocator }) {
  return (
    <div className="space-y-2">
      <p className="text-[11px] text-muted-foreground">第 {loc.slide_no} 张幻灯片</p>
      <svg
        viewBox={`0 0 ${loc.width} ${loc.height}`}
        className="w-full rounded-lg border border-border bg-white"
        role="img"
        aria-label={`第 ${loc.slide_no} 张幻灯片`}
      >
        {loc.shapes.map((shape, i) => {
          const [x, y, w, h] = shape.rect;
          return (
            <g key={i} {...(shape.hit ? { "data-shape-hit": "" } : {})}>
              <rect
                x={x}
                y={y}
                width={w}
                height={h}
                rx={2}
                fill={shape.hit ? "rgba(252, 211, 77, 0.55)" : "rgba(0, 0, 0, 0.04)"}
                stroke={shape.hit ? "rgba(217, 119, 6, 0.9)" : "rgba(0, 0, 0, 0.12)"}
              />
              <foreignObject x={x} y={y} width={w} height={h}>
                <div className="h-full w-full overflow-hidden p-1 text-[10px] leading-tight">
                  {shape.text}
                </div>
              </foreignObject>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
