"use client";

import { memo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle,
  Check,
  Copy,
  Loader2,
  Pencil,
  RefreshCw,
  Scissors,
  Sparkles,
  Square,
  ThumbsDown,
  ThumbsUp,
  UsersRound,
  WifiOff,
  X,
  Zap,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Markdown } from "@/components/markdown";
import { Citations } from "@/components/citations";
import { ResearchSteps } from "@/components/research-steps";
import { AttachmentList } from "@/components/attachments/attachment-list";
import { InlineArtifactHandle } from "@/components/artifacts/inline-artifact-handle";
import { restoreAgentGraph } from "@/hooks/useAgentRunGraph";
import { AgentInlineStatus } from "@/components/agents/agent-inline-status";
import { VoicePlayback } from "@/components/chat/voice-playback";
import { MessageVersions } from "@/components/chat/message-versions";
import { CONVERSATION_DETAIL_QUERY_KEY } from "@/hooks/useConversations";
import { useMessageFeedback } from "@/hooks/useMessageActions";
import { useChatUiStore } from "@/stores/chat-ui-store";
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
import { collectArtifactIds } from "@/lib/artifacts";
import { MemoryUsage } from "@/components/memory/memory-entry-point";

/** Hermes-mode assistant header: ⚡ badge + live status dot + memory chip.
 *  Rendered for the live stream (mode === "hermes") and for reloaded
 *  messages (metadata.hermes === true, written by the backend epilogue). */
function HermesHeader({
  streaming,
  memory,
  error,
}: {
  streaming: boolean;
  memory: boolean;
  error: boolean;
}) {
  return (
    <div className="mb-2 flex items-center gap-2">
      <span className="inline-flex items-center gap-1.5 rounded-lg border border-violet-500/30 bg-gradient-to-r from-violet-500/10 via-fuchsia-500/10 to-amber-500/10 px-2.5 py-1 text-xs font-medium text-violet-700 dark:text-violet-300">
        <span aria-hidden>⚡</span>
        Hermes Agent
        {streaming ? (
          <span className="relative ml-1 flex h-2 w-2" aria-label="运行中">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-violet-400 opacity-75" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-violet-500" />
          </span>
        ) : error ? (
          <span className="ml-1 h-2 w-2 rounded-full bg-destructive" aria-label="出错" />
        ) : (
          <span className="ml-1 h-2 w-2 rounded-full bg-emerald-500" aria-label="已完成" />
        )}
      </span>
      {memory && (
        <span
          className="inline-flex items-center gap-1 rounded-md border border-border bg-background/60 px-1.5 py-0.5 text-[10px] text-muted-foreground"
          title="Hermes 服务端会话记忆已连接（跨对话长期记忆生效）"
        >
          <span aria-hidden>🧠</span> 记忆已连接
        </span>
      )}
    </div>
  );
}

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
  onOpenMemoryManager?: () => void;
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
      className="h-9 max-sm:h-11 gap-1 px-2 text-xs text-muted-foreground"
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
        className={cn("h-9 w-9 text-muted-foreground max-sm:h-11 max-sm:w-11", up && "text-green-600 dark:text-green-400")}
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
        className={cn("h-9 w-9 text-muted-foreground max-sm:h-11 max-sm:w-11", down && "text-destructive")}
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

/** 「删除这条之后」的确认框（条目 33）。
 *
 * 不可逆操作要写清后果，而不是只挂一句「确定吗」：这里说得出的是「之后每一轮都会
 * 没掉」，以及被删内容仍能从历史版本找回 —— 少了后半句，用户就不敢按。
 */
function TruncateAfterDialog({
  open,
  onOpenChange,
  onConfirm,
  pending,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
  pending: boolean;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>删除这条提问之后的消息？</DialogTitle>
          <DialogDescription>
            之后的每一轮都会被删除，操作不可撤销。被删掉的内容会先存成历史版本，
            还能从那一条的「历史版本」里找回。
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button variant="destructive" size="sm" disabled={pending} onClick={onConfirm}>
            {pending && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
            确认删除
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function formatMessageTime(iso: string | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (sameDay) return hm;
  return `${d.toLocaleDateString([], { month: "numeric", day: "numeric" })} ${hm}`;
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
  onOpenMemoryManager,
}: MessageBubbleProps) {
  const isUser = message.role === "user";
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(message.content);
  const queryClient = useQueryClient();
  const [savingEdit, setSavingEdit] = useState(false);
  const [confirmTruncate, setConfirmTruncate] = useState(false);
  const [truncating, setTruncating] = useState(false);

  // 这三处动作都改的是服务端的那条消息行，而消息列表的真相来自会话详情缓存，
  // 所以每次成功后都要重取——只改本地 state 会在下一次刷新时被打回旧内容。
  const refreshConversation = () =>
    queryClient.invalidateQueries({
      queryKey: CONVERSATION_DETAIL_QUERY_KEY(message.conversation_id),
    });

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
    hermes?: boolean;
    hermes_memory?: boolean;
  };
  const isMultiAgent = !isUser && meta.multi_agent === true && !!meta.run_id;
  const attachments = isUser ? (meta.attachments ?? []) : [];

  // Hermes turn identification: the persisted flag (backend epilogue) for
  // reloaded messages, or the live mode for the streaming bubble.
  const liveMode = useChatUiStore((s) => s.mode);
  const isHermes =
    !isUser && (meta.hermes === true || (isStreaming && liveMode === "hermes"));
  const hermesMemory = meta.hermes_memory === true;

  // Citation integrity: strip any in-text [source N] that has no backing
  // citation. The structured citation chips (rendered below) are the source of
  // truth; the text is sanitized so a hallucinated/demo marker never shows as a
  // dangling "[source 5]". Applies to both the live stream and persisted msgs.
  const citationCount = resolvedCitations?.length ?? 0;
  const safeContent = !isUser
    ? sanitizeSourceMarkers(message.content, citationCount)
    : message.content;
  const citationFlagged = !isUser && meta.citation_validation_failed === true;

  // Artifact references — from the assistant text (`artifact:<id>` handles)
  // AND the persisted metadata list (written when a tool result spilled).
  // Both render as inline artifact cards with an authenticated download link.
  const artifactHandles = !isUser
    ? collectArtifactIds(
        message.content,
        (message.metadata as { artifacts?: unknown } | undefined)?.artifacts,
      )
    : [];

  const commitEdit = () => {
    const next = draft.trim();
    if (next && next !== message.content && onBranch) {
      onBranch(message.id, next);
    }
    setEditing(false);
  };

  const saveEditOnly = async () => {
    const next = draft.trim();
    if (!next) return;
    if (next === message.content) {
      setEditing(false);
      return;
    }
    setSavingEdit(true);
    try {
      await api.updateMessageContent(message.id, next);
      await refreshConversation();
      setEditing(false);
    } catch (e) {
      toast.error("保存失败", { description: userErrorMessage(e) });
    } finally {
      setSavingEdit(false);
    }
  };

  const truncateAfter = async () => {
    setTruncating(true);
    try {
      const res = await api.truncateMessagesAfter(message.id);
      await refreshConversation();
      setConfirmTruncate(false);
      toast.success(
        res.deleted > 0 ? `已删除这条之后的 ${res.deleted} 条消息` : "这条之后本来就没有消息"
      );
    } catch (e) {
      toast.error("删除失败", { description: userErrorMessage(e) });
    } finally {
      setTruncating(false);
    }
  };

  return (
    <div
      className={cn(
        "group flex gap-3 px-4 py-5 md:px-0",
        isUser ? "flex-row-reverse" : "flex-row"
      )}
    >
      {/* Avatar on the ASSISTANT side only (ChatGPT/DeepSeek pattern): the
          user bubble sits flush against the column's right edge so bubble,
          AI text, and composer all share one axis. */}
      {!isUser && (
        <Avatar className="h-7 w-7 shrink-0" role="img" aria-label="AI 助手">
          <AvatarFallback
            className={cn(
              isHermes
                ? "bg-gradient-to-br from-violet-500 to-fuchsia-500 text-white"
                : "bg-muted text-muted-foreground"
            )}
          >
            {isHermes ? (
              <Zap className="h-3.5 w-3.5" aria-hidden />
            ) : (
              <Sparkles className="h-3.5 w-3.5" aria-hidden />
            )}
          </AvatarFallback>
        </Avatar>
      )}

      <div
        className={cn(
          // Assistant: leave avatar(1.75rem) + gap(0.75rem) = 2.5rem for the
          // avatar. User: full width, flush right (no avatar).
          "flex min-w-0 flex-col",
          isUser ? "max-w-full items-end" : "max-w-[calc(100%-2.5rem)] items-start"
        )}
      >
        {isHermes && (
          <HermesHeader
            streaming={Boolean(isStreaming)}
            memory={hermesMemory}
            error={status === "error"}
          />
        )}

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
              {/* 两种改法差别很大，所以各给一个按钮而不是一个开关：
                  「编辑并发送」在后面另起一轮；「仅保存正文」原地改掉这一条、
                  一个字都不重新问。后者用来修错别字，不烧 token。 */}
              <Button
                variant="ghost"
                size="sm"
                className="h-7 gap-1 text-xs"
                disabled={savingEdit}
                onClick={saveEditOnly}
              >
                {savingEdit ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <Check className="h-3 w-3" />
                )}
                仅保存正文
              </Button>
              <Button size="sm" className="h-7 gap-1 text-xs" onClick={commitEdit}>
                <Check className="h-3 w-3" /> 编辑并发送
              </Button>
            </div>
          </div>
        ) : (
          isUser ? (
            <div className="w-fit max-w-full rounded-3xl rounded-br-lg bg-indigo-50 px-4 py-2.5 text-indigo-900 dark:bg-indigo-500/15 dark:text-indigo-100">
              <p className="whitespace-pre-wrap break-words text-[0.95rem] leading-[1.7]">
                {message.content}
              </p>
            </div>
          ) : message.content ? (
            <div className="w-full text-[0.95rem] leading-[1.7]">
              <Markdown
                content={safeContent}
                className={cn("text-[0.95rem] leading-[1.7]", isStreaming && "msg-streaming")}
                lite={isStreaming}
              />
              {citationFlagged && (
                <p className="mt-1 text-[11px] text-muted-foreground">
                  已自动移除缺少真实引用支持的来源标记。
                </p>
              )}
              {artifactHandles.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {artifactHandles.map((id) => (
                    <InlineArtifactHandle key={id} artifactId={id} />
                  ))}
                </div>
              )}
            </div>
          ) : (
            isStreaming && <AgentInlineStatus />
          )
        )}

        {/* Live tool steps sit AFTER the streamed text (GPT tool-use style):
            each batch is sandwiched between the narration before it and the
            narration that follows — once the next text arrives, the stream
            layer slices the batch out and the block disappears. */}
        {!isUser && !isMultiAgent && resolvedSteps && resolvedSteps.length > 0 && (
          <ResearchSteps steps={resolvedSteps} live={Boolean(isStreaming)} />
        )}

        {!isUser && resolvedCitations && resolvedCitations.length > 0 && (
          <div className="w-full">
            <Citations
              citations={resolvedCitations}
              onSourceClick={(i) => onSourceClick?.(i, resolvedCitations)}
            />
          </div>
        )}

        {!isUser && (
          <MemoryUsage
            value={message.metadata?.user_memories}
            onManage={() => onOpenMemoryManager?.()}
          />
        )}

        {/* Termination status banner (truncated / interrupted / cancelled / error). */}
        {!isUser && status && status !== "complete" && (
          <StatusBanner status={status} onContinue={onContinue} onRegenerate={onRegenerate} />
        )}

        {/* Action row */}
        {!editing && (
          <div
            className={cn(
              // Touch devices have no hover: the row must be always-visible
              // there, or copy/regenerate/feedback are undiscoverable. On
              // pointer devices the hover/focus reveal stays.
              "mt-1 flex items-center gap-1 opacity-100 transition-opacity sm:opacity-0 sm:group-hover:opacity-100 sm:focus-within:opacity-100",
              isUser ? "flex-row-reverse" : "flex-row"
            )}
          >
            {!isUser && (
              <time
                className="mr-1 text-[11px] text-muted-foreground/70"
                dateTime={message.created_at}
                title={message.created_at}
              >
                {formatMessageTime(message.created_at)}
              </time>
            )}
            {!isUser && message.content && <CopyButton text={safeContent} />}
            {/* 喇叭：播的是同一条回答（去引用标记后的文本）。流式期间不给点——
                那时文本还是半成品，而且每次点击都是后端一次花钱的合成。 */}
            {!isUser && !isStreaming && message.content && (
              <VoicePlayback messageId={message.id} content={safeContent} />
            )}
            {!isUser && !isStreaming && <FeedbackButtons messageId={message.id} />}
            {!isUser && isLast && canRegenerate && !isStreaming && (
              <Button
                variant="ghost"
                size="sm"
                className="h-9 max-sm:h-11 gap-1 px-2 text-xs text-muted-foreground"
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
                className="h-9 max-sm:h-11 gap-1 px-2 text-xs text-muted-foreground"
                onClick={() => {
                  setDraft(message.content);
                  setEditing(true);
                }}
              >
                <Pencil className="h-3 w-3" />
                编辑
              </Button>
            )}
            {/* 历史版本（条目 31）：改过 / 重新生成过的内容不再静默销毁。最后一条
                流式消息没有版本可看，所以和反馈按钮同一套条件。 */}
            {!isStreaming && message.content && (
              <MessageVersions
                message={{
                  conversationId: message.conversation_id,
                  id: message.id,
                  role: message.role,
                  content: message.content,
                  createdAt: message.created_at,
                  modelName: message.model_name,
                }}
              />
            )}
            {isUser && !isLast && (
              <Button
                variant="ghost"
                size="sm"
                className="h-9 max-sm:h-11 gap-1 px-2 text-xs text-muted-foreground"
                onClick={() => setConfirmTruncate(true)}
              >
                <Scissors className="h-3 w-3" />
                删除此后
              </Button>
            )}
          </div>
        )}
        <TruncateAfterDialog
          open={confirmTruncate}
          onOpenChange={setConfirmTruncate}
          onConfirm={truncateAfter}
          pending={truncating}
        />
      </div>
    </div>
  );
});
