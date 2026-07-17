"use client";

import { useEffect, useRef } from "react";
import { MessageSquare } from "lucide-react";

import { cn } from "@/lib/utils";
import { MessageBubble } from "@/components/message-bubble";
import type { Citation, Message, ResearchStep } from "@/lib/types";

interface MessageListProps {
  messages: Message[];
  /** Live streaming text for the in-flight assistant reply. */
  streamingText?: string;
  isStreaming?: boolean;
  /** Live citations during streaming. */
  streamingCitations?: Citation[];
  /** Live agent steps (search/thinking) during streaming. */
  streamingSteps?: ResearchStep[];
  canRegenerate?: boolean;
  onRegenerate?: () => void;
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
  className,
}: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom when messages or streaming text change.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    // Only auto-scroll if the user is near the bottom (within 120px).
    const nearBottom =
      container.scrollHeight -
        container.scrollTop -
        container.clientHeight <
      120;

    if (nearBottom) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, streamingText, isStreaming]);

  const isEmpty = messages.length === 0 && !streamingText && !isStreaming;

  // While streaming, the persisted "pending" assistant message (empty content)
  // is represented by the live streaming bubble below — drop it to avoid a
  // duplicate "思考中..." bubble alongside the live one.
  const shownMessages =
    isStreaming && streamingText !== undefined
      ? messages.filter(
          (m, i) => !(m.role === "assistant" && !m.content && i === messages.length - 1)
        )
      : messages;

  if (isEmpty) {
    return (
      <div
        className={cn(
          "flex flex-1 flex-col items-center justify-center gap-4 p-8 text-center",
          className
        )}
      >
        <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-muted">
          <MessageSquare className="h-7 w-7 text-muted-foreground" />
        </div>
        <div className="space-y-1">
          <h2 className="text-lg font-semibold">开始新的对话</h2>
          <p className="max-w-sm text-sm text-muted-foreground">
            输入你的问题，AI 将为你解答。你可以选择不同的模型与知识库来获得更精准的回答。
          </p>
        </div>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className={cn("flex-1 overflow-y-auto", className)}
    >
      <div className="mx-auto w-full max-w-3xl px-1 py-4">
        {shownMessages.map((msg, i) => (
          <MessageBubble
            key={msg.id}
            message={msg}
            isLast={i === shownMessages.length - 1 && !streamingText}
            canRegenerate={canRegenerate && !isStreaming}
            onRegenerate={onRegenerate}
          />
        ))}

        {/* Live streaming bubble */}
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
          />
        )}

        <div ref={bottomRef} className="h-4" />
      </div>
    </div>
  );
}
