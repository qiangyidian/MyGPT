"use client";

// Activity feed: a chronological, human-readable timeline of what each agent
// did (started / tool call / tool result / completed / waiting / failed).
// No raw chain-of-thought — only structured execution state.

import { useEffect, useRef } from "react";
import {
  AlertCircle,
  Check,
  Loader2,
  Wrench,
  ArrowRight,
  Clock,
} from "lucide-react";

import { cn } from "@/lib/utils";
import type { AgentGraphState, AgentGraphNode } from "@/lib/agent-graph-types";

interface FeedItem {
  key: string;
  icon: typeof Check;
  spin?: boolean;
  text: string;
  tone: "muted" | "primary" | "emerald" | "amber" | "destructive";
}

export function AgentActivityFeed({
  graph,
  className,
}: {
  graph: AgentGraphState;
  className?: string;
}) {
  const items = buildFeed(graph);
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [items.length]);

  return (
    <div className={cn("space-y-1.5", className)}>
      {items.length === 0 && (
        <p className="px-1 py-4 text-center text-xs text-muted-foreground">
          暂无执行动态
        </p>
      )}
      {items.map((it) => {
        const Icon = it.icon;
        return (
          <div
            key={it.key}
            className="flex items-start gap-2 rounded-md px-1.5 py-1 text-xs"
          >
            <Icon
              className={cn(
                "mt-0.5 h-3.5 w-3.5 shrink-0",
                it.tone === "primary" && "text-primary",
                it.tone === "emerald" && "text-emerald-600",
                it.tone === "amber" && "text-amber-600",
                it.tone === "destructive" && "text-destructive",
                it.tone === "muted" && "text-muted-foreground",
                it.spin && "animate-spin"
              )}
            />
            <span className="text-foreground/90">{it.text}</span>
          </div>
        );
      })}
      <div ref={endRef} />
    </div>
  );
}

function buildFeed(graph: AgentGraphState): FeedItem[] {
  const out: FeedItem[] = [];
  const nodeOf = (id: string) => graph.nodes.find((n) => n.id === id);

  // Order nodes by start time if available, else by stage.
  const ordered = [...graph.nodes].sort((a, b) => {
    const ta = a.startedAt ? new Date(a.startedAt).getTime() : a.stage * 1000;
    const tb = b.startedAt ? new Date(b.startedAt).getTime() : b.stage * 1000;
    return ta - tb;
  });

  for (const n of ordered) {
    if (n.status === "pending" || n.status === "queued") continue;
    if (n.startedAt) {
      out.push({
        key: `${n.id}-start`,
        icon: Loader2,
        spin: n.status === "running",
        tone: "primary",
        text: `${n.name} 开始执行：${n.taskTitle ?? n.role ?? ""}`,
      });
    }
    if (n.currentTool) {
      out.push({
        key: `${n.id}-tool-${n.currentTool.callId}`,
        icon: Wrench,
        spin: n.currentTool.status === "running",
        tone:
          n.currentTool.status === "failed"
            ? "destructive"
            : n.currentTool.status === "waiting_approval"
              ? "amber"
              : "primary",
        text: `${n.name} 调用工具 ${n.currentTool.name}${
          n.currentTool.title ? ` · ${n.currentTool.title}` : ""
        }`,
      });
      if (n.currentTool.status === "completed" || n.currentTool.status === "failed") {
        out.push({
          key: `${n.id}-toolres-${n.currentTool.callId}`,
          icon: n.currentTool.status === "completed" ? Check : AlertCircle,
          tone: n.currentTool.status === "completed" ? "emerald" : "destructive",
          text: `${n.currentTool.name} ${n.currentTool.status === "completed" ? "完成" : "失败"}`,
        });
      }
    }
    if (n.status === "waiting") {
      out.push({
        key: `${n.id}-wait`,
        icon: Clock,
        tone: "amber",
        text: `${n.name} 等待中`,
      });
    }
    if (n.status === "completed") {
      // Handoff to downstream.
      const downstream = graph.edges
        .filter((e) => e.source === n.id && e.status === "completed")
        .map((e) => nodeOf(e.target)?.name)
        .filter(Boolean);
      out.push({
        key: `${n.id}-done`,
        icon: Check,
        tone: "emerald",
        text:
          downstream.length > 0
            ? `${n.name} 完成 → 移交给 ${downstream.join("、")}`
            : `${n.name} 完成`,
      });
    }
    if (n.status === "failed") {
      out.push({
        key: `${n.id}-fail`,
        icon: AlertCircle,
        tone: "destructive",
        text: `${n.name} 失败${n.error ? `：${n.error}` : ""}`,
      });
    }
    if (n.status === "cancelled") {
      out.push({
        key: `${n.id}-cancel`,
        icon: ArrowRight,
        tone: "muted",
        text: `${n.name} 已取消`,
      });
    }
  }

  return out;
}
