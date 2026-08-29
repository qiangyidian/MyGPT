"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { GitBranch, Loader2 } from "lucide-react";

import { api } from "@/lib/api";
import type { Conversation } from "@/lib/types";
import { relativeTime } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface BranchesResponse {
  parent: Conversation | null;
  children: Conversation[];
}

/**
 * Branch tree entry: lists the parent + sibling branches of a conversation
 * (real backend: GET /api/conversations/{id}/branches) and navigates on click.
 * Previously branch points existed but had NO UI to return to a parent.
 */
export function BranchHistory({
  conversationId,
  activeConversationId,
  onNavigate,
  className,
}: {
  conversationId: string;
  activeConversationId: string | null;
  onNavigate: (id: string) => void;
  className?: string;
}) {
  const [open, setOpen] = useState(false);

  const q = useQuery<BranchesResponse>({
    queryKey: ["conversation-branches", conversationId],
    queryFn: () => api.listConversationBranches(conversationId),
    enabled: open,
  });

  const hasBranches =
    !!q.data && (!!q.data.parent || (q.data.children?.length ?? 0) > 0);
  if (!hasBranches && !q.isLoading) return null;

  return (
    <>
      <Button
        variant="ghost"
        size="sm"
        className={className}
        onClick={() => setOpen(true)}
        aria-label="查看分支历史"
      >
        <GitBranch className="h-3 w-3" /> 分支
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>分支历史</DialogTitle>
            <DialogDescription>
              本对话由编辑消息产生分支时，可在此返回任意版本。
            </DialogDescription>
          </DialogHeader>
          <div className="max-h-[50vh] space-y-1.5 overflow-y-auto">
            {q.isLoading ? (
              <div className="flex items-center justify-center gap-2 py-6 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" /> 加载分支…
              </div>
            ) : (
              <>
                {q.data?.parent && (
                  <BranchRow
                    conv={q.data.parent}
                    active={q.data.parent.id === activeConversationId}
                    badge="父版本"
                    onClick={() => {
                      onNavigate(q.data!.parent!.id);
                      setOpen(false);
                    }}
                  />
                )}
                {q.data?.children?.map((c) => (
                  <BranchRow
                    key={c.id}
                    conv={c}
                    active={c.id === activeConversationId}
                    badge="分支"
                    onClick={() => {
                      onNavigate(c.id);
                      setOpen(false);
                    }}
                  />
                ))}
              </>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

function BranchRow({
  conv,
  active,
  badge,
  onClick,
}: {
  conv: Conversation;
  active: boolean;
  badge: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        "flex w-full items-center gap-2 rounded-lg border px-3 py-2 text-left text-sm transition-colors " +
        (active
          ? "border-primary/40 bg-primary/5"
          : "border-border hover:bg-accent")
      }
    >
      <span className="min-w-0 flex-1">
        <span className="block truncate font-medium">
          {conv.title || "新对话"}
        </span>
        <span className="block text-[11px] text-muted-foreground">
          {relativeTime(conv.updated_at)}
          {conv.last_message_preview ? ` · ${conv.last_message_preview.slice(0, 40)}` : ""}
        </span>
      </span>
      <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
        {active ? "当前" : badge}
      </span>
    </button>
  );
}
