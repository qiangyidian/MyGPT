"use client";

import { Download, Network } from "lucide-react";
import { useQuery } from "@tanstack/react-query";

import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { AgentFlowGraph } from "@/components/agents/agent-flow-graph";
import { AgentActivityFeed } from "@/components/agents/agent-activity-feed";
import { useAgentRunStore } from "@/stores/agent-run-store";

const STATUS_LABEL: Record<string, string> = {
  pending: "等待中",
  running: "运行中",
  waiting_approval: "等待确认",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const STATUS_DOT: Record<string, string> = {
  running: "bg-primary",
  waiting_approval: "bg-amber-500",
  completed: "bg-green-500",
  failed: "bg-destructive",
  cancelled: "bg-muted-foreground",
  pending: "bg-muted-foreground",
};

/**
 * The Execution tab: real multi-agent lifecycle (graph + activity) sourced
 * entirely from the backend-driven agent-run-store — no frontend simulation.
 */
export function ExecutionTab() {
  const active = useAgentRunStore((s) => s.active);
  const clock = useAgentRunStore((s) => s.clock); // re-render each second for live durations
  const now = Date.now();
  void clock;

  if (!active.runId || active.nodes.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 py-12 text-center text-xs text-muted-foreground">
        <Network className="h-5 w-5" />
        <span>暂无执行过程。深度研究运行后，这里会展示实时链路。</span>
      </div>
    );
  }

  const running = active.activeAgentIds.length;
  const total = active.nodes.length;

  return (
    <ScrollArea className="h-full">
      <div className="flex items-center justify-between px-3 py-2">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <span className={cn("h-2 w-2 rounded-full", STATUS_DOT[active.status] ?? "bg-muted-foreground")} />
          <span>{STATUS_LABEL[active.status] ?? active.status}</span>
          <span>· {running > 0 ? `${running} / ${total} 进行中` : `${total} 个步骤`}</span>
        </div>
      </div>
      <div className="px-3 pb-3">
        <AgentFlowGraph nodes={active.nodes} edges={active.edges} now={now} />
        <div className="mt-4">
          <AgentActivityFeed graph={active} />
        </div>
        <ToolCallAudit runId={active.runId} />
      </div>
    </ScrollArea>
  );
}

/** Persisted tool-call audit trail + JSON export (the on-prem "what ran?" view). */
function ToolCallAudit({ runId }: { runId: string }) {
  const { data: run } = useQuery({
    queryKey: ["agent-run-detail", runId],
    queryFn: () => api.getAgentRun(runId),
    refetchInterval: 2000,
  });
  if (!run) return null;
  const calls = run.tool_calls ?? [];
  const exportJson = () => {
    const blob = new Blob([JSON.stringify(run, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `run-${runId}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };
  return (
    <div className="mt-4">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          工具调用审计 ({calls.length})
        </span>
        <Button
          variant="ghost"
          size="sm"
          className="h-6 gap-1 px-2 text-[11px]"
          onClick={exportJson}
        >
          <Download className="h-3 w-3" /> 导出 JSON
        </Button>
      </div>
      {calls.length === 0 ? (
        <p className="px-1 text-[11px] text-muted-foreground">本次运行未调用工具。</p>
      ) : (
        <ul className="space-y-1">
          {calls.map((c) => (
            <li key={c.id} className="rounded-md border border-border bg-card p-2 text-[11px]">
              <div className="flex items-center justify-between">
                <span className="font-medium">{c.tool_name}</span>
                <span className={cn("text-[10px]", c.status === "success" ? "text-green-600" : "text-destructive")}>
                  {c.status}
                </span>
              </div>
              <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-all text-muted-foreground">
                {JSON.stringify(c.arguments)}
                {"\n→ "}
                {c.error_message ?? JSON.stringify(c.result)}
              </pre>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
