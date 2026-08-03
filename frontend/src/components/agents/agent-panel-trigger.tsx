"use client";

// The multi-agent panel trigger, shown at the top-right of the chat area.
// Visible whenever there's an active multi-agent run (≥2 nodes), even if the
// panel was dismissed (so the user can reopen it, including for finished runs).
// A pulsing dot appears when ≥1 agent is running; the label shows running count.

import { AlertTriangle, UsersRound } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  selectRuntimeFallback,
  selectShouldShowPanel,
  useAgentRunStore,
} from "@/stores/agent-run-store";

export function AgentPanelTrigger({ className }: { className?: string }) {
  const active = useAgentRunStore((s) => s.active);
  const open = selectShouldShowPanel(useAgentRunStore.getState());
  const fallback = selectRuntimeFallback(useAgentRunStore.getState());
  const dismissActive = useAgentRunStore((s) => s.dismissActive);
  const reopenActive = useAgentRunStore((s) => s.reopenActive);

  const total = active.nodes.length;
  const running = active.activeAgentIds.length;
  const multi = total >= 2;

  // Fallback: a multi-agent request couldn't run for real. Show a warning badge
  // (NOT a fake agent panel — there are no nodes) so the user knows.
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

  if (!multi) return null;

  const label =
    running > 0 ? `${running} 个运行中` : `${total} 个 Agent`;

  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant={open ? "default" : "outline"}
            size="sm"
            aria-label={`多 Agent 协作：${total} 个 Agent${
              running > 0 ? `，${running} 个运行中` : ""
            }。${open ? "点击关闭面板" : "点击打开面板"}`}
            aria-pressed={open}
            onClick={() => (open ? dismissActive() : reopenActive())}
            className={cn("h-8 gap-1.5 text-xs font-medium", className)}
          >
            <UsersRound className="h-4 w-4" />
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
          {open ? "关闭多 Agent 面板" : "查看多 Agent 执行过程"}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
