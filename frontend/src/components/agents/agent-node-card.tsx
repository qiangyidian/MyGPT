"use client";

// One agent node in the flow graph. Renders role, task, status, duration, and
// the current tool nested under the agent (with its own mini status). The
// running state uses a restrained pulse (respects prefers-reduced-motion via
// the .reduce-motion utility in globals.css).

import { useState } from "react";
import { ChevronDown, Wrench } from "lucide-react";

import { cn } from "@/lib/utils";
import type { AgentGraphNode } from "@/lib/agent-graph-types";
import { AgentStatusBadge, AgentStatusDot } from "./agent-status-badge";

const TOOL_ICON: Record<string, string> = {
  web_search: "搜索",
  http_get: "网页",
  python_exec: "代码",
  db_query: "数据库",
  file_analyze: "文件",
  datetime_now: "时间",
};

function formatDuration(ms?: number): string {
  if (!ms && ms !== 0) return "";
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
  const m = Math.floor(s / 60);
  return `${m}m${Math.round(s % 60)}s`;
}

export function AgentNodeCard({
  node,
  now,
  className,
}: {
  node: AgentGraphNode;
  /** Shared clock tick value (ms epoch or counter) to recompute live duration. */
  now?: number;
  className?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const running = node.status === "running";
  const hasDetail = !!(node.taskSummary || node.outputSummary || node.error);
  // Live duration while running: startedAt -> now; else stored durationMs.
  let durationLabel = "";
  if (node.status === "running" && node.startedAt) {
    const live = (now ?? Date.now()) - new Date(node.startedAt).getTime();
    durationLabel = formatDuration(live > 0 ? live : 0);
  } else {
    durationLabel = formatDuration(node.durationMs);
  }

  const tool = node.currentTool;

  return (
    <div
      role="listitem"
      aria-label={`${node.name}，${statusLabel(node.status)}${durationLabel ? `，已运行 ${durationLabel}` : ""}`}
      className={cn(
        "relative w-[200px] rounded-lg border bg-card p-3 text-left shadow-sm transition-colors",
        "border-border",
        running && "border-primary ring-1 ring-primary/30 agent-node-running",
        node.status === "waiting" && "border-amber-500/50",
        node.status === "failed" && "border-destructive/50",
        node.status === "completed" && "border-emerald-500/40",
        className
      )}
    >
      <div className="flex items-center gap-2">
        <AgentStatusDot status={node.status} />
        <span className="truncate text-sm font-semibold text-foreground">{node.name}</span>
      </div>

      {node.role && (
        <div className="mt-0.5 truncate text-[11px] text-muted-foreground">{node.role}</div>
      )}

      {node.taskTitle && (
        <div className="mt-2 text-xs text-foreground/90 line-clamp-2">{node.taskTitle}</div>
      )}

      {/* Current tool nested under the agent */}
      {tool && (
        <div className="mt-2 flex items-center gap-1.5 rounded-md bg-muted/60 px-2 py-1 text-[11px]">
          <Wrench className={cn("h-3 w-3 text-muted-foreground", tool.status === "running" && "animate-spin")} />
          <span className="font-medium text-foreground">
            {TOOL_ICON[tool.name] ?? tool.name}
          </span>
          {tool.title && (
            <span className="truncate text-muted-foreground">· {tool.title}</span>
          )}
          {tool.status === "waiting_approval" && (
            <span className="ml-auto text-amber-600">待确认</span>
          )}
        </div>
      )}

      <div className="mt-2 flex items-center justify-between gap-2">
        <AgentStatusBadge status={node.status} />
        {durationLabel && (
          <span className="text-[11px] tabular-nums text-muted-foreground">{durationLabel}</span>
        )}
      </div>

      {hasDetail && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="mt-1.5 inline-flex items-center gap-1 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
          aria-expanded={expanded}
        >
          <ChevronDown className={cn("h-3 w-3 transition-transform", expanded && "rotate-180")} />
          {expanded ? "收起" : "详情"}
        </button>
      )}
      {expanded && (
        <div className="mt-1.5 space-y-1 text-[11px]">
          {node.taskSummary && (
            <div className="text-muted-foreground">{node.taskSummary}</div>
          )}
          {node.error && <div className="break-words text-destructive">{node.error}</div>}
          {node.outputSummary && (
            <div className="break-words text-muted-foreground">{node.outputSummary}</div>
          )}
        </div>
      )}
    </div>
  );
}

function statusLabel(s: AgentGraphNode["status"]): string {
  const map: Record<AgentGraphNode["status"], string> = {
    pending: "等待执行",
    queued: "排队中",
    running: "正在执行",
    waiting: "等待中",
    completed: "已完成",
    failed: "已失败",
    cancelled: "已取消",
  };
  return map[s];
}
