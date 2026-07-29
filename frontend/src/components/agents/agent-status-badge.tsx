"use client";

// Status visual mapping for an agent node. Color + icon + text together (never
// color alone, per the a11y requirement). Used by AgentNodeCard and the header.

import {
  AlertCircle,
  Ban,
  Check,
  Clock,
  Loader2,
  ShieldAlert,
  Circle,
} from "lucide-react";

import { cn } from "@/lib/utils";
import type { AgentNodeStatus } from "@/lib/agent-graph-types";

export const NODE_STATUS_META: Record<
  AgentNodeStatus,
  { label: string; icon: typeof Check; wrap: string; dot: string }
> = {
  pending: {
    label: "等待执行",
    icon: Circle,
    wrap: "border-border text-muted-foreground bg-muted/30",
    dot: "bg-muted-foreground",
  },
  queued: {
    label: "排队中",
    icon: Circle,
    wrap: "border-border text-muted-foreground bg-muted/30",
    dot: "bg-muted-foreground",
  },
  running: {
    label: "正在执行",
    icon: Loader2,
    wrap: "border-primary text-primary bg-primary/5",
    dot: "bg-primary",
  },
  waiting: {
    label: "等待中",
    icon: Clock,
    wrap: "border-amber-500/50 text-amber-600 bg-amber-500/5",
    dot: "bg-amber-500",
  },
  completed: {
    label: "已完成",
    icon: Check,
    wrap: "border-emerald-500/50 text-emerald-600 bg-emerald-500/5",
    dot: "bg-emerald-500",
  },
  failed: {
    label: "已失败",
    icon: AlertCircle,
    wrap: "border-destructive/50 text-destructive bg-destructive/5",
    dot: "bg-destructive",
  },
  cancelled: {
    label: "已取消",
    icon: Ban,
    wrap: "border-border text-muted-foreground/70 bg-muted/20 opacity-70",
    dot: "bg-muted-foreground/50",
  },
};

export function AgentStatusBadge({
  status,
  className,
}: {
  status: AgentNodeStatus;
  className?: string;
}) {
  const meta = NODE_STATUS_META[status];
  const Icon = meta.icon;
  const spinning = status === "running";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium",
        meta.wrap,
        className
      )}
    >
      <Icon className={cn("h-3 w-3", spinning && "animate-spin")} />
      {meta.label}
    </span>
  );
}

export function AgentStatusDot({ status, className }: { status: AgentNodeStatus; className?: string }) {
  const meta = NODE_STATUS_META[status];
  return (
    <span className={cn("relative flex h-2.5 w-2.5", className)}>
      {status === "running" && (
        <span className={cn("absolute inline-flex h-full w-full animate-ping rounded-full opacity-60", meta.dot)} />
      )}
      <span className={cn("relative inline-flex h-2.5 w-2.5 rounded-full", meta.dot)} />
    </span>
  );
}

export { ShieldAlert };
