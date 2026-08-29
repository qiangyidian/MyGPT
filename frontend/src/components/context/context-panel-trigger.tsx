"use client";

import { FileText, Loader2, UsersRound } from "lucide-react";
import { useQuery } from "@tanstack/react-query";

import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useAgentRunStore } from "@/stores/agent-run-store";
import { useAttachmentStore } from "@/stores/attachment-store";
import { useContextPanelStore } from "@/stores/context-panel-store";
import type { ContextTab } from "@/lib/types";

/**
 * Dynamic Context Panel trigger. Instead of a fixed "多 Agent" button, the
 * label/icon reflect what's actually available right now (running agents,
 * citations, files). Hidden when there's nothing to show.
 */
export function ContextPanelTrigger({
  conversationId,
  hasPendingApproval,
  className,
}: {
  conversationId: string | null;
  hasPendingApproval?: boolean;
  className?: string;
}) {
  const active = useAgentRunStore((s) => s.active);
  const sourcesCount = useContextPanelStore((s) => s.sources.length);
  const open = useContextPanelStore((s) => s.open);
  const currentTab = useContextPanelStore((s) => s.tab);
  const openWith = useContextPanelStore((s) => s.openWith);
  const close = useContextPanelStore((s) => s.close);

  const running = active.activeAgentIds.length;
  const hasGraph = !!active.runId && active.nodes.length >= 1;
  // Count the CONVERSATION's attachments (what the Files tab actually lists),
  // plus any in-composer drafts the user is about to send — the badge and the
  // panel now agree on what "文件 N" means.
  const draftsCount = useAttachmentStore((s) =>
    conversationId ? s.drafts[conversationId]?.length ?? 0 : 0
  );
  const { data: convAttachments } = useQuery({
    queryKey: ["chat-attachments", conversationId ?? ""],
    queryFn: () =>
      conversationId ? api.listChatAttachments(conversationId) : Promise.resolve([]),
    enabled: !!conversationId,
  });
  const filesCount = draftsCount + (convAttachments?.length ?? 0);

  // Priority: live execution/approval > citations > files.
  let tab: ContextTab | null = null;
  let label = "";
  let busy = false;
  if (running > 0 || hasPendingApproval) {
    tab = "execution";
    label = running > 0 ? `${running} 个运行中` : "等待确认";
    busy = running > 0;
  } else if (hasGraph) {
    tab = "execution";
    label = "查看执行";
  } else if (sourcesCount > 0) {
    tab = "sources";
    label = `来源 ${sourcesCount}`;
  } else if (filesCount > 0) {
    tab = "files";
    label = `文件 ${filesCount}`;
  }

  if (!tab) return null;

  const Icon = tab === "sources" ? FileText : UsersRound;
  const isThisTabOpen = open && currentTab === tab;

  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant={isThisTabOpen ? "default" : "outline"}
            size="sm"
            aria-label={label}
            aria-pressed={isThisTabOpen}
            onClick={() => (isThisTabOpen ? close() : openWith(tab!))}
            className={cn("h-8 gap-1.5 text-xs font-medium", className)}
          >
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Icon className="h-4 w-4" />}
            <span>{label}</span>
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">
          {isThisTabOpen ? "关闭上下文面板" : `查看${tab === "execution" ? "执行过程" : tab === "sources" ? "来源" : "文件"}`}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
