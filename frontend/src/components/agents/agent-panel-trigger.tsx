"use client";

// The agent-panel trigger pill, shown at the top of the chat area.
//
// Visible whenever there's an active agent run worth looking at: a genuine
// multi-agent crew (≥2 nodes — even when finished, so it can be reopened), or a
// single-agent (native) turn while it's still running. A pulsing dot appears
// while ≥1 agent is running; the label distinguishes multi-agent vs the
// single assistant. Clicking toggles the right-side Context Panel onto the
// Execution tab (shares the panel store with ContextPanelTrigger).

import { AlertTriangle, Bot, UsersRound } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  isRunFinished,
  selectHasAgentRun,
  selectIsMultiAgent,
  selectRunningCount,
  selectRuntimeFallback,
  useAgentRunStore,
} from "@/stores/agent-run-store";
import { useContextPanelStore } from "@/stores/context-panel-store";

export function AgentPanelTrigger({ className }: { className?: string }) {
  const active = useAgentRunStore((s) => s.active);
  const fallback = useAgentRunStore(selectRuntimeFallback);
  const hasRun = useAgentRunStore(selectHasAgentRun);
  const multi = useAgentRunStore(selectIsMultiAgent);
  const running = useAgentRunStore(selectRunningCount);
  const finished = isRunFinished(active);
  const panelOpen = useContextPanelStore((s) => s.open);
  const openWith = useContextPanelStore((s) => s.openWith);
  const close = useContextPanelStore((s) => s.close);

  // Fallback: a multi-agent request couldn't run for real — warn, don't fake.
  if (fallback) {
    return (
      <TooltipProvider delayDuration={300}>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="outline"
              size="sm"
              aria-label="多 Agent 运行时不可用，已回退为普通模式"
              className={cn(
                "h-8 gap-1.5 text-xs font-medium border-amber-500/50 text-amber-600 dark:text-amber-400",
                className
              )}
            >
              <AlertTriangle className="h-4 w-4" />
              <span>多 Agent 不可用</span>
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom" className="max-w-xs">
            多 Agent 运行时不可用，本次已回退为普通模式。
            {fallback.fallbackReason ? `原因：${fallback.fallbackReason}。` : ""}
            请在根目录 .env 启用 CREWAI_ENABLED=true 或 AGENT_DEMO_MODE=true 并重启后端。
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  // Show for any viewable run: multi-agent (even finished) or a running single.
  const show = hasRun && (multi || !finished);
  if (!show) return null;

  const total = active.nodes.length;
  const label = multi
    ? running > 0
      ? `多 Agent · ${running} 运行中`
      : `多 Agent · ${total} 个`
    : "助手 · 运行中";
  const Icon = multi ? UsersRound : Bot;

  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant={panelOpen ? "default" : "outline"}
            size="sm"
            aria-label={`${label}。${panelOpen ? "点击关闭面板" : "点击查看执行过程"}`}
            aria-pressed={panelOpen}
            onClick={() => (panelOpen ? close(active.runId) : openWith("execution"))}
            className={cn("h-8 gap-1.5 text-xs font-medium", className)}
          >
            <Icon className="h-4 w-4" />
            <span>{label}</span>
            {running > 0 && (
              <span className="relative ml-0.5 flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary opacity-60" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-primary" />
              </span>
            )}
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">
          {panelOpen ? "关闭执行面板" : "查看执行过程"}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
