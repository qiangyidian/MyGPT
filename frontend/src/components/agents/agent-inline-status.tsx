"use client";

import { useAgentRunStore } from "@/stores/agent-run-store";
import { useContextPanelStore } from "@/stores/context-panel-store";
import type { AgentGraphNode, AgentGraphState } from "@/lib/agent-graph-types";

/**
 * Derive the one-line status label. Pure so it can be unit-tested directly
 * (the component is store-driven and therefore not renderable in the node-only
 * vitest environment, which has no server snapshot for the zustand store).
 *
 * 运行中显示真实进度（心跳带来的最近工具），完成后显示完成而非「并行中」——
 * 数据全部来自 store，不编造前端模拟的计数或耗时。
 */
export function inlineStatusLabel(active: AgentGraphState): string {
  const multi = active.nodes.length >= 2;

  if (!active.runId || active.nodes.length === 0) return "思考中…";
  if (!multi) return "智能助手正在作答";

  const runningNodes = active.activeAgentIds
    .map((id) => active.nodes.find((n) => n.id === id))
    .filter((n): n is AgentGraphNode => !!n);
  if (runningNodes.length > 0) {
    const head = runningNodes[0];
    const note = head.progressNote ? ` · ${head.progressNote}` : "";
    return runningNodes.length > 1
      ? `${runningNodes.map((n) => n.name).join("、")} 并行中${note}`
      : `${head.name} 处理中${note}`;
  }

  const finished = active.nodes.filter((n) => n.status === "completed");
  if (finished.length > 0) {
    const last = finished[finished.length - 1];
    const secs =
      last.durationMs != null ? ` · ${(last.durationMs / 1000).toFixed(1)}s` : "";
    return `✓ ${last.name} 完成${secs}`;
  }
  return "多 Agent 协作中";
}

/**
 * Live agent status shown inside the streaming assistant bubble before tokens
 * arrive — the long pre-answer phase of a multi-agent run, or the start of a
 * native turn. Falls back to a gentle "思考中…" until the graph lands. Clicking
 * opens the Execution tab so the user can follow the chain.
 */
export function AgentInlineStatus() {
  const active = useAgentRunStore((s) => s.active);
  const openWith = useContextPanelStore((s) => s.openWith);

  if (!active.runId || active.nodes.length === 0) {
    return (
      <span
        className="inline-flex items-center gap-1 text-sm text-muted-foreground"
        aria-live="polite"
      >
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
        思考中…
      </span>
    );
  }

  const label = inlineStatusLabel(active);

  return (
    <button
      type="button"
      onClick={() => openWith("execution")}
      className="inline-flex items-center gap-1.5 rounded-md bg-primary/5 px-2 py-1 text-xs text-primary transition-colors hover:bg-primary/10"
      aria-live="polite"
    >
      <span className="flex gap-0.5">
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary" />
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary [animation-delay:150ms]" />
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary [animation-delay:300ms]" />
      </span>
      <span className="font-medium">{label}</span>
      <span className="text-primary/70">展开 ▸</span>
    </button>
  );
}
