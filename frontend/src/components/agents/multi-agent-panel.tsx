"use client";

// Multi-agent collaboration panel. Desktop (xl+): a docked right column that
// shrinks the chat area. Below xl: a Radix Sheet overlay with a backdrop.
//
// Visibility is driven by the store (selectShouldShowPanel): the panel appears
// when a multi-agent run (≥2 nodes) is active and the user hasn't dismissed it.
// A new run clears the dismissal; dismissing the current run keeps it closed
// until a different run starts.

import { useEffect, useState } from "react";
import { AlertTriangle, Loader2, Network } from "lucide-react";

import { cn } from "@/lib/utils";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Sheet,
  SheetContent,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  selectRuntimeFallback,
  selectShouldShowPanel,
  useAgentRunStore,
} from "@/stores/agent-run-store";
import { useAgentRunGraph } from "@/hooks/useAgentRunGraph";
import { AgentRunHeader } from "./agent-run-header";
import { AgentFlowGraph } from "./agent-flow-graph";
import { AgentActivityFeed } from "./agent-activity-feed";

// SSR-safe breakpoint hook: xl (1280px) and up = desktop dock.
function useIsDesktop() {
  const [isDesktop, setIsDesktop] = useState(false);
  useEffect(() => {
    const mql = window.matchMedia("(min-width: 1280px)");
    const onChange = () => setIsDesktop(mql.matches);
    onChange();
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);
  return isDesktop;
}

export function MultiAgentPanel() {
  const active = useAgentRunStore((s) => s.active);
  const clock = useAgentRunStore((s) => s.clock); // re-render each second for live durations
  const dismissActive = useAgentRunStore((s) => s.dismissActive);
  const show = selectShouldShowPanel(useAgentRunStore.getState());
  const [tab, setTab] = useState<"flow" | "activity">("flow");
  const isDesktop = useIsDesktop();
  // Drive the shared clock + poll fallback while mounted.
  useAgentRunGraph();

  const now = Date.now();
  void clock;

  const body = (
    <div className="flex min-h-0 flex-1 flex-col">
      <AgentRunHeader graph={active} now={now} onClose={dismissActive} />
      <Tabs value={tab} onValueChange={(v) => setTab(v as "flow" | "activity")} className="flex min-h-0 flex-1 flex-col">
        <div className="px-3 pt-2">
          <TabsList className="grid w-full grid-cols-2">
            <TabsTrigger value="flow" className="text-xs">协作链路</TabsTrigger>
            <TabsTrigger value="activity" className="text-xs">执行动态</TabsTrigger>
          </TabsList>
        </div>
        <ScrollArea className="min-h-0 flex-1">
          <TabsContent value="flow" className="m-0 p-3">
            {active.nodes.length === 0 ? (
              <LoadingState />
            ) : (
              <AgentFlowGraph nodes={active.nodes} edges={active.edges} now={now} />
            )}
          </TabsContent>
          <TabsContent value="activity" className="m-0 p-3">
            <AgentActivityFeed graph={active} />
          </TabsContent>
        </ScrollArea>
      </Tabs>
    </div>
  );

  // ---- Desktop dock: part of the flex layout, shrinks the chat area ----
  if (isDesktop) {
    if (!show) return null;
    return (
      <aside
        aria-label="多 Agent 协作面板"
        className="flex w-[400px] min-w-[340px] max-w-[480px] shrink-0 flex-col border-l border-border bg-card animate-in slide-in-from-right duration-200"
      >
        {body}
      </aside>
    );
  }

  // ---- Mobile / narrow: overlay Sheet ----
  return (
    <Sheet open={show} onOpenChange={(o) => { if (!o) dismissActive(); }}>
      <SheetContent side="right" className="w-[88vw] sm:max-w-[460px]">
        <SheetTitle className="sr-only">多 Agent 协作</SheetTitle>
        <SheetDescription className="sr-only">
          多个 Agent 协作执行的实时状态与链路。
        </SheetDescription>
        {body}
      </SheetContent>
    </Sheet>
  );
}

function LoadingState() {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-12 text-center text-xs text-muted-foreground">
      <Loader2 className="h-5 w-5 animate-spin text-primary" />
      <span>正在初始化 Agent 拓扑…</span>
      <span className="inline-flex items-center gap-1 text-[11px]">
        <Network className="h-3 w-3" /> 等待 agent_graph 事件
      </span>
    </div>
  );
}

// Re-export for convenience so page.tsx imports from one place if needed.
export { cn };
