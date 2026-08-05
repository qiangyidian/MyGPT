"use client";

import { memo, useState } from "react";
import {
  AlertTriangle,
  Check,
  Copy,
  Pencil,
  RefreshCw,
  Scissors,
  Sparkles,
  Square,
  ThumbsDown,
  ThumbsUp,
  User as UserIcon,
  UsersRound,
  WifiOff,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Markdown } from "@/components/markdown";
import { Citations } from "@/components/citations";
import { ResearchSteps } from "@/components/research-steps";
import { AttachmentList } from "@/components/attachments/attachment-list";
import { restoreAgentGraph } from "@/hooks/useAgentRunGraph";
import { AgentInlineStatus } from "@/components/agents/agent-inline-status";
import { useMessageFeedback } from "@/hooks/useMessageActions";
import type {
  AgentStep,
  AttachmentRef,
  Citation,
  GenerationStatus,
  Message,
  ResearchStep,
} from "@/lib/types";
import { getMessageStatus } from "@/lib/types";
import { sanitizeSourceMarkers } from "@/lib/citations";

interface MessageBubbleProps {
  message: Message;
  isLast: boolean;
  citations?: Citation[];
  steps?: ResearchStep[];
  isStreaming?: boolean;
  canRegenerate?: boolean;
  onRegenerate?: () => void;
  /** Continue a truncated/interrupted/cancelled answer (new turn, no repeat). */
  onContinue?: () => void;
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
      aria-label={copied ? "已复制" : "复制"}
    >
      {copied ? <Check className="h-3 w-3 text-green-500 dark:text-green-400" /> : <Copy className="h-3 w-3" />}
      {copied ? "已复制" : "复制"}
    </Button>
  );
}

const STATUS_CONFIG: Partial<
  Record<GenerationStatus, { label: string; className: string; action: "continue" | "retry"; Icon: LucideIcon }>
> = {
  truncated: {
    label: "输出达到长度上限，内容可能不完整",
    className: "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400",
    action: "continue",
    Icon: Scissors,
  },
  interrupted: {
    label: "连接中断，已保留已生成内容",
    className: "border-orange-500/40 bg-orange-500/10 text-orange-700 dark:text-orange-400",
    action: "continue",
    Icon: WifiOff,
  },
  cancelled: {
    label: "已停止，已保留已生成内容",
    className: "border-border bg-muted/50 text-muted-foreground",
    action: "continue",
    Icon: Square,
  },
  error: {
    label: "生成失败",
    className: "border-destructive/40 bg-destructive/10 text-destructive",
    action: "retry",
    Icon: AlertTriangle,
  },
};

function StatusBanner({
  status,
  onContinue,
  onRegenerate,
}: {
  status: GenerationStatus;
  onContinue?: () => void;
  onRegenerate?: () => void;
}) {
  const cfg = STATUS_CONFIG[status];
  if (!cfg) return null;
  const Icon = cfg.Icon;
  return (
    <div className={cn("mt-1.5 flex items-center gap-2 rounded-md border px-3 py-1.5 text-xs", cfg.className)}>
      <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
      <span>{cfg.label}</span>
      {cfg.action === "continue" && onContinue && (
        <button
          type="button"
          onClick={onContinue}
          className="ml-auto font-medium underline-offset-2 hover:underline"
        >
          继续生成
        </button>
      )}
      {cfg.action === "retry" && onRegenerate && (
        <button
          type="button"
          onClick={onRegenerate}
          className="ml-auto font-medium underline-offset-2 hover:underline"
        >
          重试
        </button>
      )}
    </div>
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
        className={cn("h-7 w-7 text-muted-foreground", up && "text-green-600 dark:text-green-400")}
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
  onContinue,
  onBranch,
  onSourceClick,
  onOpenAttachment,
}: MessageBubbleProps) {
  const isUser = message.role === "user";
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(message.content);

  // Terminal status for an assistant message (null while streaming / complete).
  const status = !isUser && !isStreaming ? getMessageStatus(message) : null;

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
    citation_validation_failed?: boolean;
  };
  const isMultiAgent = !isUser && meta.multi_agent === true && !!meta.run_id;
  const attachments = isUser ? (meta.attachments ?? []) : [];

  // Citation integrity: strip any in-text [source N] that has no backing
  // citation. The structured citation chips (rendered below) are the source of
  // truth; the text is sanitized so a hallucinated/demo marker never shows as a
  // dangling "[source 5]". Applies to both the live stream and persisted msgs.
  const citationCount = resolvedCitations?.length ?? 0;
  const safeContent = !isUser
    ? sanitizeSourceMarkers(message.content, citationCount)
    : message.content;
  const citationFlagged = !isUser && meta.citation_validation_failed === true;

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
      <Avatar className="h-7 w-7 shrink-0" role="img" aria-label={isUser ? "你的消息" : "AI 助手"}>
        <AvatarFallback
          className={cn(
            isUser ? "bg-indigo-500 text-white" : "bg-muted text-muted-foreground"
          )}
        >
          {isUser ? <UserIcon className="h-3.5 w-3.5" aria-hidden /> : <Sparkles className="h-3.5 w-3.5" aria-hidden />}
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
          isUser ? (
            <div className="w-fit max-w-[85%] rounded-2xl rounded-br-md bg-indigo-50 px-4 py-2.5 text-indigo-900 dark:bg-indigo-500/15 dark:text-indigo-100">
              <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">
                {message.content}
              </p>
            </div>
          ) : message.content ? (
            <div className="w-full text-[0.95rem] leading-[1.7]">
              <Markdown
                content={safeContent}
                className={cn("text-[0.95rem] leading-[1.7]", isStreaming && "msg-streaming")}
              />
              {citationFlagged && (
                <p className="mt-1 text-[11px] text-muted-foreground">
                  已自动移除缺少真实引用支持的来源标记。
                </p>
              )}
            </div>
          ) : (
            isStreaming && <AgentInlineStatus />
          )
        )}

        {!isUser && resolvedCitations && resolvedCitations.length > 0 && (
          <div className="w-full">
            <Citations
              citations={resolvedCitations}
              onSourceClick={(i) => onSourceClick?.(i, resolvedCitations)}
            />
          </div>
        )}

        {/* Termination status banner (truncated / interrupted / cancelled / error). */}
        {!isUser && status && status !== "complete" && (
          <StatusBanner status={status} onContinue={onContinue} onRegenerate={onRegenerate} />
        )}

        {/* Action row */}
        {!editing && (
          <div
            className={cn(
              "mt-1 flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100",
              isUser ? "flex-row-reverse" : "flex-row"
            )}
          >
            {!isUser && message.content && <CopyButton text={safeContent} />}
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
