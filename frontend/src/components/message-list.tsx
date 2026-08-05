"use client";

import { useEffect, useRef } from "react";
import { BarChart3, FileSearch, MessageSquare, PenLine, Search } from "lucide-react";

import { cn } from "@/lib/utils";
import { MessageBubble } from "@/components/message-bubble";
import type { Citation, Message, ResearchStep } from "@/lib/types";

const SUGGESTIONS = [
  { icon: FileSearch, title: "总结文档", prompt: "请帮我总结一份文档的主要观点。" },
  { icon: Search, title: "搜索最新资料", prompt: "帮我搜索某个主题的最新资料并给出带来源的汇总。" },
  { icon: BarChart3, title: "分析数据", prompt: "我上传了一份表格，请帮我分析其中的趋势与异常。" },
  { icon: PenLine, title: "编写方案", prompt: "请帮我起草一份项目方案的初稿。" },
];

interface MessageListProps {
  messages: Message[];
  streamingText?: string;
  isStreaming?: boolean;
  streamingCitations?: Citation[];
  streamingSteps?: ResearchStep[];
  canRegenerate?: boolean;
  onRegenerate?: () => void;
  /** Continue a truncated/interrupted/cancelled answer (new turn, no repeat). */
  onContinue?: () => void;
  onBranch?: (messageId: string, newContent: string) => void;
  onSourceClick?: (index: number, citations: Citation[]) => void;
  onOpenAttachment?: (attachmentId: string) => void;
  onPickSuggestion?: (prompt: string) => void;
  className?: string;
}

export function MessageList({
  messages,
  streamingText,
  isStreaming,
  streamingCitations,
  streamingSteps,
  canRegenerate,
  onRegenerate,
  onContinue,
  onBranch,
  onSourceClick,
  onOpenAttachment,
  onPickSuggestion,
  className,
}: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const nearBottom =
      container.scrollHeight - container.scrollTop - container.clientHeight < 120;
    if (nearBottom) {
      const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
      bottomRef.current?.scrollIntoView({ behavior: reduce ? "auto" : "smooth" });
    }
  }, [messages, streamingText, isStreaming]);

  const isEmpty = messages.length === 0 && !streamingText && !isStreaming;

  const shownMessages =
    isStreaming && streamingText !== undefined
      ? messages.filter(
          (m, i) => !(m.role === "assistant" && !m.content && i === messages.length - 1)
        )
      : messages;

  if (isEmpty) {
    return (
      <div className={cn("flex flex-1 flex-col items-center justify-center gap-5 p-8 text-center", className)}>
        <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-muted">
          <MessageSquare className="h-7 w-7 text-muted-foreground" />
        </div>
        <div className="space-y-1">
          <h2 className="text-lg font-semibold">有什么可以帮你？</h2>
          <p className="max-w-sm text-sm text-muted-foreground">
            选择合适的能力，或直接描述你的目标。系统会自动选择最合适的方式。
          </p>
        </div>
        {onPickSuggestion && (
          <div className="grid w-full max-w-xl grid-cols-1 gap-2 sm:grid-cols-2">
            {SUGGESTIONS.map((s) => {
              const Icon = s.icon;
              return (
                <button
                  key={s.title}
                  type="button"
                  onClick={() => onPickSuggestion(s.prompt)}
                  className="flex items-center gap-2.5 rounded-lg border border-border bg-card px-3 py-2.5 text-left text-sm transition-colors hover:bg-accent"
                >
                  <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
                  <span className="font-medium">{s.title}</span>
                </button>
              );
            })}
          </div>
        )}
      </div>
    );
  }

  return (
    <div ref={containerRef} className={cn("min-h-0 flex-1 overflow-y-auto", className)}>
      <div className="mx-auto w-full max-w-3xl px-1 py-6">
        {shownMessages.map((msg, i) => (
          <MessageBubble
            key={msg.id}
            message={msg}
            isLast={i === shownMessages.length - 1 && !streamingText}
            canRegenerate={canRegenerate && !isStreaming}
            onRegenerate={onRegenerate}
            onContinue={onContinue}
            onBranch={onBranch}
            onSourceClick={onSourceClick}
            onOpenAttachment={onOpenAttachment}
          />
        ))}

        {streamingText !== undefined && isStreaming && (
          <MessageBubble
            message={{
              id: "streaming",
              conversation_id: "",
              role: "assistant",
              content: streamingText,
              metadata: {},
              model_name: null,
              created_at: new Date().toISOString(),
            }}
            citations={streamingCitations}
            steps={streamingSteps}
            isStreaming
            isLast
            onSourceClick={onSourceClick}
          />
        )}

        <div ref={bottomRef} className="h-4" />
      </div>
    </div>
  );
}
