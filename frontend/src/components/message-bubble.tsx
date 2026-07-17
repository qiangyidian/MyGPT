"use client";

import { memo, useState } from "react";
import { Check, Copy, RefreshCw, User as UserIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Markdown } from "@/components/markdown";
import { Citations } from "@/components/citations";
import { ResearchSteps } from "@/components/research-steps";
import type { AgentStep, Citation, Message, ResearchStep } from "@/lib/types";

interface MessageBubbleProps {
  message: Message;
  isLast: boolean;
  /** Citations passed in from the message metadata (for persisted messages). */
  citations?: Citation[];
  /** Agent steps (search/thinking) for the live streaming bubble. */
  steps?: ResearchStep[];
  /** Whether this is the currently-streaming assistant message (live text). */
  isStreaming?: boolean;
  /** Show the "重新生成" button. */
  canRegenerate?: boolean;
  onRegenerate?: () => void;
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
    >
      {copied ? (
        <Check className="h-3 w-3 text-green-500" />
      ) : (
        <Copy className="h-3 w-3" />
      )}
      {copied ? "已复制" : "复制"}
    </Button>
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
}: MessageBubbleProps) {
  const isUser = message.role === "user";

  // Resolve citations: prefer prop, then fall back to metadata.
  const resolvedCitations =
    citations ??
    (Array.isArray(message.metadata?.citations)
      ? (message.metadata!.citations as Citation[])
      : undefined);

  // Resolve agent steps: prefer prop (live), then fall back to metadata. Older
  // persisted steps used the legacy {name, arguments, result} shape; normalize
  // them to the current AgentStep model so the panel renders either cleanly.
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

  return (
    <div
      className={cn(
        "group flex gap-3 px-4 py-5 md:px-0",
        isUser ? "flex-row-reverse" : "flex-row"
      )}
    >
      {/* Avatar */}
      <Avatar className="h-8 w-8 shrink-0">
        <AvatarFallback
          className={cn(
            isUser
              ? "bg-primary text-primary-foreground"
              : "bg-secondary text-secondary-foreground"
          )}
        >
          {isUser ? <UserIcon className="h-4 w-4" /> : "AI"}
        </AvatarFallback>
      </Avatar>

      {/* Content column */}
      <div
        className={cn(
          "flex min-w-0 max-w-[calc(100%-3rem)] flex-col",
          isUser ? "items-end" : "items-start"
        )}
      >
        {/* Agent steps (search / thinking) — shown above the answer, OpenAI-style */}
        {!isUser && resolvedSteps && resolvedSteps.length > 0 && (
          <ResearchSteps steps={resolvedSteps} />
        )}

        <div
          className={cn(
            "w-full overflow-hidden rounded-lg px-4 py-3",
            isUser
              ? "bg-primary text-primary-foreground"
              : "bg-muted/50 text-foreground"
          )}
        >
          {isUser ? (
            <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">
              {message.content}
            </p>
          ) : message.content ? (
            <div className="text-sm">
              <Markdown content={message.content} />
            </div>
          ) : (
            isStreaming && (
              <span className="inline-flex items-center gap-1 text-sm text-muted-foreground">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
                思考中...
              </span>
            )
          )}
        </div>

        {/* Citations for assistant messages */}
        {!isUser && resolvedCitations && resolvedCitations.length > 0 && (
          <div className="mt-1 w-full">
            <Citations citations={resolvedCitations} />
          </div>
        )}

        {/* Action row */}
        <div
          className={cn(
            "mt-1 flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100",
            isUser ? "flex-row-reverse" : "flex-row"
          )}
        >
          {!isUser && message.content && <CopyButton text={message.content} />}
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
        </div>
      </div>
    </div>
  );
});
