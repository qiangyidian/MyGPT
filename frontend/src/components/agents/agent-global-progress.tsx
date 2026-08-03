"use client";

import { isRunFinished, useAgentRunStore } from "@/stores/agent-run-store";
import { useContextPanelStore } from "@/stores/context-panel-store";

/**
 * A small floating progress badge (bottom-left, past the sidebar on desktop):
 * a ring showing completed/total agents + a running-count line. Mounted in
 * AppShell so an active run is perceivable even at a glance. Clicking opens the
 * Execution tab. Only visible while a run is in flight (hidden when finished).
 */
export function AgentGlobalProgress() {
  const active = useAgentRunStore((s) => s.active);
  const openWith = useContextPanelStore((s) => s.openWith);

  if (!active.runId || active.nodes.length === 0) return null;
  if (isRunFinished(active)) return null;

  const total = active.nodes.length;
  const done = active.nodes.filter((n) => n.status === "completed").length;
  const running = active.activeAgentIds.length;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;

  return (
    <button
      type="button"
      onClick={() => openWith("execution")}
      className="fixed bottom-4 left-4 z-40 flex items-center gap-2 rounded-full border border-border bg-card/95 px-2.5 py-1.5 shadow-md backdrop-blur transition-colors hover:bg-accent md:left-72"
      aria-label={`Agent 运行中：${done}/${total} 完成，${running} 个进行中。点击查看执行过程`}
    >
      <span
        className="relative flex h-7 w-7 items-center justify-center rounded-full"
        style={{
          background: `conic-gradient(hsl(var(--primary)) ${pct}%, hsl(var(--muted)) 0)`,
        }}
      >
        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-card text-[9px] font-bold tabular-nums text-foreground">
          {pct}
        </span>
      </span>
      <span className="pr-1 text-left text-[11px] leading-tight">
        <span className="block font-medium text-foreground">
          {done}/{total} 完成
        </span>
        {running > 0 && <span className="block text-primary">{running} 运行中</span>}
      </span>
    </button>
  );
}
