"use client";

import { selectIsMultiAgent, useAgentRunStore } from "@/stores/agent-run-store";
import { useContextPanelStore } from "@/stores/context-panel-store";
import type { AgentGraphNode } from "@/lib/agent-graph-types";

/**
 * Live agent status shown inside the streaming assistant bubble before tokens
 * arrive — the long pre-answer phase of a multi-agent run, or the start of a
 * native turn. Falls back to a gentle "思考中…" until the graph lands. Clicking
 * opens the Execution tab so the user can follow the chain.
 */
export function AgentInlineStatus() {
  const active = useAgentRunStore((s) => s.active);
  const multi = useAgentRunStore(selectIsMultiAgent);
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

  const runningNodes = active.activeAgentIds
    .map((id) => active.nodes.find((n) => n.id === id))
    .filter((n): n is AgentGraphNode => !!n);

  const label = multi
    ? runningNodes.length > 0
      ? `${runningNodes.map((n) => n.name).join("、")} 并行中`
      : "多 Agent 协作中"
    : "智能助手正在作答";

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
