"use client";

// Lightweight multi-agent DAG renderer.
//
// Layout: nodes are grouped into horizontal layers by `stage` (top → bottom).
// Within a stage, nodes are placed side-by-side by `lane` (left → right) —
// that's how parallel agents render on the same row. SVG polylines draw the
// edges between node anchor points; the edges are recomputed on resize via a
// ResizeObserver so they stay glued to the cards.
//
// This deliberately avoids a heavyweight graph library (xyflow etc.): the node
// count is 2–8, there's no need for pan/zoom/edit, and SVG + CSS grid keeps the
// bundle tiny and the layout predictable.

import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { cn } from "@/lib/utils";
import type {
  AgentGraphEdge,
  AgentGraphNode,
  AgentEdgeStatus,
} from "@/lib/agent-graph-types";
import { AgentNodeCard } from "./agent-node-card";

const NODE_W = 200;
const ROW_GAP = 56; // vertical gap between stages (room for the connector)
const COL_GAP = 16;

const EDGE_STYLE: Record<AgentEdgeStatus, { stroke: string; dash: string; width: number; animate: boolean }> = {
  pending: { stroke: "hsl(var(--border))", dash: "0", width: 1.5, animate: false },
  active: { stroke: "hsl(var(--primary))", dash: "6 4", width: 2, animate: true },
  completed: { stroke: "hsl(142 71% 45%)", dash: "0", width: 2, animate: false },
  failed: { stroke: "hsl(var(--destructive))", dash: "0", width: 2, animate: false },
};

interface NodeBox {
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

export function AgentFlowGraph({
  nodes,
  edges,
  now,
  className,
}: {
  nodes: AgentGraphNode[];
  edges: AgentGraphEdge[];
  now?: number;
  className?: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [boxes, setBoxes] = useState<Record<string, NodeBox>>({});
  const [size, setSize] = useState({ w: 0, h: 0 });

  // Group nodes by stage.
  const stages = groupByStage(nodes);

  // Measure node positions after layout (and on resize).
  useLayoutEffect(() => {
    const measure = () => {
      const root = containerRef.current;
      if (!root) return;
      const next: Record<string, NodeBox> = {};
      let maxBottom = 0;
      root.querySelectorAll<HTMLElement>("[data-node-id]").forEach((el) => {
        const id = el.dataset.nodeId!;
        const r = el.getBoundingClientRect();
        const rootRect = root.getBoundingClientRect();
        next[id] = {
          id,
          x: r.left - rootRect.left + r.width / 2,
          y: r.top - rootRect.top,
          w: r.width,
          h: r.height,
        };
        maxBottom = Math.max(maxBottom, r.bottom - rootRect.top);
      });
      setBoxes(next);
      setSize({ w: root.clientWidth, h: maxBottom });
    };
    measure();
    const ro = new ResizeObserver(measure);
    if (containerRef.current) ro.observe(containerRef.current);
    return () => ro.disconnect();
  }, [nodes.length, stages.length]);

  return (
    <div ref={containerRef} className={cn("relative", className)}>
      {/* SVG edge layer (absolute, behind nodes) */}
      <svg
        className="pointer-events-none absolute inset-0 h-full w-full"
        width={size.w}
        height={size.h}
        aria-hidden="true"
      >
        {edges.map((e) => {
          const from = boxes[e.source];
          const to = boxes[e.target];
          if (!from || !to) return null;
          const style = EDGE_STYLE[e.status];
          const x1 = from.x;
          const y1 = from.y + from.h;
          const x2 = to.x;
          const y2 = to.y;
          const midY = (y1 + y2) / 2;
          const d = `M ${x1} ${y1} C ${x1} ${midY}, ${x2} ${midY}, ${x2} ${y2}`;
          return (
            <g key={e.id}>
              <path
                d={d}
                fill="none"
                stroke={style.stroke}
                strokeWidth={style.width}
                strokeDasharray={style.dash}
                strokeLinecap="round"
                className={cn(style.animate && "agent-edge-active")}
              />
            </g>
          );
        })}
      </svg>

      {/* Node layers */}
      <div className="relative space-y-[ROW_GAP]" style={{ ["--ROW_GAP" as string]: `${ROW_GAP}px` }}>
        {stages.map((row, i) => (
          <div
            key={i}
            className={cn(
              "flex flex-wrap justify-center gap-[COL_GAP]",
              row.length > 1 ? "gap-x-4" : ""
            )}
            style={{ gap: `${COL_GAP}px`, marginBottom: `${ROW_GAP}px` }}
          >
            {row.map((node) => (
              <div key={node.id} data-node-id={node.id} style={{ width: NODE_W }}>
                <AgentNodeCard node={node} now={now} />
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

function groupByStage(nodes: AgentGraphNode[]): AgentGraphNode[][] {
  const byStage = new Map<number, AgentGraphNode[]>();
  for (const n of nodes) {
    const arr = byStage.get(n.stage) ?? [];
    arr.push(n);
    byStage.set(n.stage, arr);
  }
  return [...byStage.keys()]
    .sort((a, b) => a - b)
    .map((stage) => byStage.get(stage)!.sort((a, b) => (a.lane ?? 0) - (b.lane ?? 0)));
}

// Keep ROW_GAP referenced for the CSS var (avoids dead-code lint).
void ROW_GAP;
