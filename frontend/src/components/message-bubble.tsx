"use client";

import { memo, useState } from "react";
import {
  Check,
  Copy,
  Pencil,
  RefreshCw,
  ThumbsDown,
  ThumbsUp,
  User as UserIcon,
  UsersRound,
  X,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Markdown } from "@/components/markdown";
import { Citations } from "@/components/citations";
import { ResearchSteps } from "@/components/research-steps";
import { AttachmentList } from "@/components/attachments/attachment-list";
import { restoreAgentGraph } from "@/hooks/useAgentRunGraph";
import { useMessageFeedback } from "@/hooks/useMessageActions";
import type { AgentStep, AttachmentRef, Citation, Message, ResearchStep } from "@/lib/types";

interface MessageBubbleProps {
  message: Message;
  isLast: boolean;
  citations?: Citation[];
  steps?: ResearchStep[];
  isStreaming?: boolean;
  canRegenerate?: boolean;
  onRegenerate?: () => void;
  /** Edit-and-resend: fork at this user message with edited content. */
  onBranch?: (messageId: string, newContent: string) => void;
  /** Open the Sources tab focused on a citation index (carries the citations). */
  onSourceClick?: (index: number, citations: Citation[]) => void;
  /** Open the Files tab focused on an attachment id. */
  onOpenAttachment?: (attachmentId: string) => void;
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* ignore */
    }
  };
  return (
    <Button
      variant="ghost"
      size="sm"
      className="h-7 gap-1 px-2 text-xs text-muted-foreground"
      onClick={handleCopy}
      aria-label="复制"
    >
      {copied ? <Check className="h-3 w-3 text-green-500" /> : <Copy className="h-3 w-3" />}
      {copied ? "已复制" : "复制"}
    </Button>
  );
}

function FeedbackButtons({ messageId }: { messageId: string }) {
  const { feedback, set, clear, isLoading } = useMessageFeedback(messageId);
  const up = feedback?.rating === "up";
  const down = feedback?.rating === "down";
  return (
    <div className="flex items-center gap-0.5">
      <Button
        variant="ghost"
        size="icon"
        className={cn("h-7 w-7 text-muted-foreground", up && "text-green-600")}
        disabled={isLoading}
        onClick={() => (up ? void clear() : void set("up"))}
        aria-label="有帮助"
        aria-pressed={up}
      >
        <ThumbsUp className="h-3.5 w-3.5" />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className={cn("h-7 w-7 text-muted-foreground", down && "text-destructive")}
        disabled={isLoading}
        onClick={() => (down ? void clear() : void set("down"))}
        aria-label="无帮助"
        aria-pressed={down}
      >
        <ThumbsDown className="h-3.5 w-3.5" />
      </Button>
    </div>
  );
}

export const MessageBubble = memo(function MessageBubble({
  message,
  isLast,
  citations,
  steps,
  isStreaming,
  canRegenerate,
  onRegenerate,
  onBranch,
  onSourceClick,
  onOpenAttachment,
}: MessageBubbleProps) {
  const isUser = message.role === "user";
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(message.content);

  const resolvedCitations =
    citations ??
    (Array.isArray(message.metadata?.citations)
      ? (message.metadata!.citations as Citation[])
      : undefined);

  const rawSteps =
    steps ??
    (Array.isArray(message.metadata?.steps)
      ? (message.metadata!.steps as ResearchStep[])
      : undefined);
  const resolvedSteps = rawSteps?.map((s, i) =>
    s.type
      ? s
      : {
          id: s.id ?? `legacy-${i}`,
          sequence: i,
          type: "tool" as const,
          title: (s as { name?: string }).name ?? "工具",
          status: ((s as { status?: string }).status ?? "done") as AgentStep["status"],
          tool: {
            name: (s as { name?: string }).name ?? "",
            argumentsPreview: (s as { arguments?: Record<string, unknown> }).arguments,
            resultPreview: (s as { result?: string }).result,
          },
        }
  );

  const meta = (message.metadata ?? {}) as {
    multi_agent?: boolean;
    run_id?: string;
    attachments?: AttachmentRef[];
  };
  const isMultiAgent = !isUser && meta.multi_agent === true && !!meta.run_id;
  const attachments = isUser ? (meta.attachments ?? []) : [];

  const commitEdit = () => {
    const next = draft.trim();
    if (next && next !== message.content && onBranch) {
      onBranch(message.id, next);
    }
    setEditing(false);
  };

  return (
    <div
      className={cn(
        "group flex gap-3 px-4 py-5 md:px-0",
        isUser ? "flex-row-reverse" : "flex-row"
      )}
    >
      <Avatar className="h-8 w-8 shrink-0">
        <AvatarFallback
          className={cn(
            isUser ? "bg-primary text-primary-foreground" : "bg-secondary text-secondary-foreground"
          )}
        >
          {isUser ? <UserIcon className="h-4 w-4" /> : "AI"}
        </AvatarFallback>
      </Avatar>

      <div
        className={cn(
          "flex min-w-0 max-w-[calc(100%-3rem)] flex-col",
          isUser ? "items-end" : "items-start"
        )}
      >
        {!isUser && isMultiAgent && meta.run_id && (
          <button
            type="button"
            onClick={() => void restoreAgentGraph(meta.run_id!)}
            className="mb-2 inline-flex items-center gap-1.5 rounded-lg border border-border bg-background/60 px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <UsersRound className="h-3.5 w-3.5" />
            查看执行过程
          </button>
        )}

        {!isUser && !isMultiAgent && resolvedSteps && resolvedSteps.length > 0 && (
          <ResearchSteps steps={resolvedSteps} />
        )}

        {/* User attachments */}
        {isUser && attachments.length > 0 && (
          <AttachmentList
            attachments={attachments}
            onPreview={(id) => onOpenAttachment?.(id)}
            className="mb-1.5 grid w-full grid-cols-1 gap-1.5 sm:grid-cols-2"
          />
        )}

        {editing && isUser ? (
          <div className="w-full max-w-xl">
            <Textarea
              autoFocus
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              className="min-h-[80px] bg-background text-sm"
            />
            <div className="mt-1 flex justify-end gap-1">
              <Button variant="ghost" size="sm" className="h-7 gap-1 text-xs" onClick={() => setEditing(false)}>
                <X className="h-3 w-3" /> 取消
              </Button>
              <Button size="sm" className="h-7 gap-1 text-xs" onClick={commitEdit}>
                <Check className="h-3 w-3" /> 编辑并发送
              </Button>
            </div>
          </div>
        ) : (
          <div
            className={cn(
              "w-full overflow-hidden rounded-lg px-4 py-3",
              isUser ? "bg-primary text-primary-foreground" : "bg-muted/50 text-foreground"
            )}
          >
            {isUser ? (
              <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">{message.content}</p>
            ) : message.content ? (
              <div className="text-sm">
                <Markdown content={message.content} />
              </div>
            ) : (
              isStreaming && (
                <span className="inline-flex items-center gap-1 text-sm text-muted-foreground" aria-live="polite">
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
                  思考中…
                </span>
              )
            )}
          </div>
        )}

        {!isUser && resolvedCitations && resolvedCitations.length > 0 && (
          <div className="mt-1 w-full">
            <Citations
              citations={resolvedCitations}
              onSourceClick={(i) => onSourceClick?.(i, resolvedCitations)}
            />
          </div>
        )}

        {/* Action row */}
        {!editing && (
          <div
            className={cn(
              "mt-1 flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100",
              isUser ? "flex-row-reverse" : "flex-row"
            )}
          >
            {!isUser && message.content && <CopyButton text={message.content} />}
            {!isUser && !isStreaming && <FeedbackButtons messageId={message.id} />}
            {!isUser && isLast && canRegenerate && !isStreaming && (
              <Button
                variant="ghost"
                size="sm"
                className="h-7 gap-1 px-2 text-xs text-muted-foreground"
                onClick={onRegenerate}
              >
                <RefreshCw className="h-3 w-3" />
                重新生成
              </Button>
            )}
            {isUser && onBranch && (
              <Button
                variant="ghost"
                size="sm"
                className="h-7 gap-1 px-2 text-xs text-muted-foreground"
                onClick={() => {
                  setDraft(message.content);
                  setEditing(true);
                }}
              >
                <Pencil className="h-3 w-3" />
                编辑
              </Button>
            )}
          </div>
        )}
      </div>
    </div>
  );
});
