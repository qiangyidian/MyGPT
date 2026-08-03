"use client";

// Panel header: flow name, runtime, overall status, agent count + running
// count, elapsed time. The elapsed time uses the shared store clock (passed in
// as `now`) so only one timer drives every duration in the panel.

import { Cpu, Users, X, Clock } from "lucide-react";

import { cn } from "@/lib/utils";
import type { AgentGraphState } from "@/lib/agent-graph-types";

const RUN_STATUS_META: Record<AgentGraphState["status"], { label: string; cls: string }> = {
  pending: { label: "准备中", cls: "text-muted-foreground bg-muted/40" },
  running: { label: "执行中", cls: "text-primary bg-primary/10" },
  waiting_approval: { label: "等待确认", cls: "text-amber-600 bg-amber-500/10" },
  completed: { label: "已完成", cls: "text-emerald-600 bg-emerald-500/10" },
  failed: { label: "已失败", cls: "text-destructive bg-destructive/10" },
  cancelled: { label: "已取消", cls: "text-muted-foreground bg-muted/40" },
};

function elapsed(g: AgentGraphState, now?: number): string {
  const start = g.startedAt ? new Date(g.startedAt).getTime() : null;
  const end = g.finishedAt ? new Date(g.finishedAt).getTime() : null;
  if (!start) return "";
  const ms = (end ?? now ?? Date.now()) - start;
  if (ms < 0 || isNaN(ms)) return "";
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  return `${m} 分 ${s % 60} 秒`;
}

export function AgentRunHeader({
  graph,
  now,
  onClose,
  className,
}: {
  graph: AgentGraphState;
  now?: number;
  onClose?: () => void;
  className?: string;
}) {
  const meta = RUN_STATUS_META[graph.status] ?? RUN_STATUS_META.pending;
  const running = graph.activeAgentIds.length;
  const total = graph.nodes.length;
  const multi = total >= 2;
  const flowLabel = flowNameLabel(graph.flowName);

  return (
    <div className={cn("flex flex-col gap-2 border-b border-border p-3", className)}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold text-foreground">
            {multi ? "多 Agent 协作" : "智能助手"}
          </h2>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-muted-foreground">
            <span className="truncate">{flowLabel}</span>
            <span aria-hidden>·</span>
            <span className="inline-flex items-center gap-0.5">
              <Cpu className="h-3 w-3" /> {runtimeLabel(graph.runtime)}
            </span>
            <span aria-hidden>·</span>
            <span className="inline-flex items-center gap-0.5 rounded bg-muted/60 px-1 py-px font-medium text-foreground/80">
              {modeLabel(graph.mode)}
            </span>
          </div>
        </div>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭面板"
            className="rounded-md p-1 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <X className="h-4 w-4" />
          </button>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <span className={cn("inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-medium", meta.cls)}>
          {meta.label}
        </span>
        <span className="inline-flex items-center gap-1 text-muted-foreground">
          <Users className="h-3 w-3" />
          {total} 个 Agent
          {running > 0 && (
            <span className="font-medium text-primary">· {running} 个运行中</span>
          )}
        </span>
        <span className="inline-flex items-center gap-1 text-muted-foreground">
          <Clock className="h-3 w-3" />
          {elapsed(graph, now) || "—"}
        </span>
      </div>
    </div>
  );
}

function flowNameLabel(flow: string): string {
  const map: Record<string, string> = {
    deep_research: "深度研究",
    parallel_research: "并行研究",
    debate: "辩论",
    single_agent: "单 Agent 对话",
  };
  return map[flow] ?? flow ?? "";
}

function runtimeLabel(runtime: string): string {
  if (runtime === "crewai") return "CrewAI";
  if (runtime === "native") return "原生";
  return runtime;
}

function modeLabel(mode: AgentGraphState["mode"]): string {
  const map: Record<AgentGraphState["mode"], string> = {
    sequential: "顺序",
    parallel: "并行",
    hybrid: "混合",
  };
  return map[mode] ?? mode;
}
