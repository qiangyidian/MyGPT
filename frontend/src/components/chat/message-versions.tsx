"use client";

import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { GitCompare, History, Loader2 } from "lucide-react";

import { api } from "@/lib/api";
import {
  buildVersionDiff,
  describeVersion,
  isRestorable,
  isSameContent,
  sortVersionsNewestFirst,
  VERSION_DIFF_DIRECTION,
  versionPreview,
  versionsForMessage,
  type MessageVersion,
} from "@/lib/message-versions";
import { userErrorMessage } from "@/lib/api-error";
import { MarkdownDiff } from "@/components/markdown-diff";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

/** 挂到某条消息上的历史版本集合。 */
export interface VersionedMessage {
  conversationId: string;
  id: string;
  role: string;
  content: string;
  createdAt: string;
  modelName?: string | null;
}

/**
 * 版本历史入口（条目 31）：重新生成会删掉上一条回答、编辑会原地覆盖正文，两者以
 * 前都是静默销毁。这里把被换下来的内容列出来，并允许切回、查看它与当前正文的差异。
 *
 * 没有任何历史版本时整个入口不渲染——一个点开是空的按钮只会让人觉得功能坏了。
 */
export function MessageVersions({
  message,
  className,
}: {
  message: VersionedMessage;
  className?: string;
}) {
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  // 差异一次只看一版，和 busyId 同一套「记住一个 id」的状态形状。
  const [diffId, setDiffId] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const query = useQuery<MessageVersion[]>({
    queryKey: ["conversation-versions", message.conversationId],
    queryFn: () => api.listConversationVersions(message.conversationId),
  });

  const group = sortVersionsNewestFirst(
    versionsForMessage(query.data ?? [], {
      id: message.id,
      role: message.role,
      created_at: message.createdAt,
    })
  );
  const diffTarget = group.find((version) => version.id === diffId) ?? null;
  // diffLines 是这个面板里最贵的一步，只在对话框打开时算，并且别跟着无关重渲染
  // 重跑。必须留在下面的提前 return 之前：钩子顺序不能随版本数量变化。
  const diffView = useMemo(
    () => (diffTarget ? buildVersionDiff(diffTarget.content, message.content) : null),
    [diffTarget, message.content]
  );
  if (!query.isLoading && group.length === 0) return null;

  async function restore(version: MessageVersion) {
    if (busyId) return;
    setBusyId(version.id);
    setError(null);
    try {
      await api.activateMessageVersion(
        message.conversationId,
        message.id,
        version.id
      );
      // 正文换人了：会话详情和版本列表都要重取，否则界面停在旧内容上。
      await queryClient.invalidateQueries({
        queryKey: ["conversation", message.conversationId],
      });
      await queryClient.invalidateQueries({
        queryKey: ["conversation-versions", message.conversationId],
      });
    } catch (e) {
      setError(userErrorMessage(e));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <>
      <DropdownMenu onOpenChange={(open) => open && setError(null)}>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="sm"
            className={className}
            aria-label="历史版本"
            title={error ?? "历史版本"}
          >
            {query.isLoading ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <History className="h-4 w-4" />
            )}
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-[320px]">
          <DropdownMenuLabel>历史版本</DropdownMenuLabel>
          {error && (
            <div className="px-2 py-1 text-xs text-destructive">{error}</div>
          )}
          {group.map((version) => (
            <DropdownMenuGroup key={version.id}>
              <DropdownMenuItem
                disabled={
                  busyId !== null ||
                  !isRestorable(version) ||
                  isSameContent(version, message)
                }
                onSelect={() => restore(version)}
                className="flex-col items-start gap-0.5"
              >
                <span className="text-xs text-muted-foreground">
                  {describeVersion(version, group)}
                </span>
                <span className="line-clamp-2 text-sm">
                  {versionPreview(version.content)}
                </span>
              </DropdownMenuItem>
              {/* 差异不摊在列表里：一版几十行，展开三条就把这个 320px 的面板撑爆，
                  所以点开才进对话框看。 */}
              <DropdownMenuItem
                className="py-1 text-xs text-muted-foreground"
                onSelect={() => setDiffId(version.id)}
              >
                <GitCompare />
                与当前版本比对
              </DropdownMenuItem>
            </DropdownMenuGroup>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
      <Dialog
        open={diffTarget !== null}
        onOpenChange={(open) => !open && setDiffId(null)}
      >
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            {/* 标题一直有词：关对话框时 Radix 还要播一段退场动画，那时 diffTarget
                已经是 null，空标题会被读屏软件当成一个没名字的对话框。 */}
            <DialogTitle>
              与当前版本比对
              {diffTarget ? ` · ${describeVersion(diffTarget, group)}` : ""}
            </DialogTitle>
            <DialogDescription>{VERSION_DIFF_DIRECTION}</DialogDescription>
          </DialogHeader>
          {diffTarget && diffView ? (
            diffView.kind === "diff" ? (
              <div className="max-h-[65vh] overflow-y-auto rounded-lg border border-black/5 bg-[#0b1021] dark:border-white/10">
                <MarkdownDiff rows={diffView.rows} />
              </div>
            ) : (
              <div className="space-y-2">
                <p className="text-xs text-muted-foreground">{diffView.note}</p>
                {diffView.kind === "too-large" && (
                  <p className="max-h-[50vh] overflow-y-auto rounded-md bg-muted/40 p-3 text-sm leading-relaxed">
                    {versionPreview(diffTarget.content)}
                  </p>
                )}
              </div>
            )
          ) : null}
        </DialogContent>
      </Dialog>
    </>
  );
}
