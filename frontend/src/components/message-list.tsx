"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowDown, BarChart3, FileSearch, MessageSquare, PenLine, Search } from "lucide-react";

import { cn } from "@/lib/utils";
import { getVisibleMessages } from "@/lib/message-visibility";
import { MessageBubble } from "@/components/message-bubble";
import type { Citation, Message, ResearchStep } from "@/lib/types";

const SUGGESTIONS = [
  { icon: FileSearch, title: "总结文档", prompt: "请帮我总结一份文档的主要观点。" },
  { icon: Search, title: "搜索最新资料", prompt: "帮我搜索某个主题的最新资料并给出带来源的汇总。" },
  { icon: BarChart3, title: "分析数据", prompt: "我想分析一份表格数据（可先上传文件），请告诉我你需要哪些信息。" },
  { icon: PenLine, title: "编写方案", prompt: "请帮我起草一份项目方案的初稿。" },
];

/** Within this distance of the bottom counts as "at the bottom" (follow on). */
const NEAR_BOTTOM_PX = 120;

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
  /** Owner conversation — switching resets follow mode and parks at the latest message. */
  conversationId?: string | null;
  /**
   * Increment to force-scroll to the newest message. Sending is an explicit
   * "jump to the answer" intent, so it scrolls even when the user was
   * scrolled up reading history (and re-engages follow for the new run).
   * Signal pattern (not a boolean) so repeat sends retrigger.
   */
  scrollToBottomSignal?: number;
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
  conversationId,
  scrollToBottomSignal,
  className,
}: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  // Follow mode: the view tracks the newest message as it streams in. The ref
  // is the source of truth for scroll handlers; `stuck` mirrors it for render
  // (the jump button).
  const stickRef = useRef(true);
  const [stuck, setStuck] = useState(true);
  // True while our own smooth scroll animates — its intermediate positions
  // must not be read as "the user scrolled away".
  const programmaticScrollRef = useRef(false);

  const distanceFromBottom = useCallback(() => {
    const c = containerRef.current;
    return c ? c.scrollHeight - c.scrollTop - c.clientHeight : 0;
  }, []);

  const scrollToBottom = useCallback((smooth = true) => {
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const behavior = smooth && !reduce ? "smooth" : "auto";
    if (behavior === "smooth") {
      programmaticScrollRef.current = true;
      // Cleared on landing (distance ~0) or by this timeout at the latest.
      window.setTimeout(() => { programmaticScrollRef.current = false; }, 800);
    }
    bottomRef.current?.scrollIntoView({ behavior });
  }, []);

  const follow = useCallback((on: boolean) => {
    stickRef.current = on;
    setStuck(on);
  }, []);

  const isEmpty = messages.length === 0 && !streamingText && !isStreaming;

  // User scroll intent: leaving the bottom region cancels follow for THIS
  // run; returning to it re-enables follow. (ChatGPT/豆包-style: 手动上滑
  // 停止跟随，滑回底部自动恢复。)
  useEffect(() => {
    const c = containerRef.current;
    if (!c) return;
    const onScroll = () => {
      if (programmaticScrollRef.current) {
        // Our own animation — only a landing-at-bottom event counts.
        if (distanceFromBottom() < 4) follow(true);
        return;
      }
      follow(distanceFromBottom() < NEAR_BOTTOM_PX);
    };
    c.addEventListener("scroll", onScroll, { passive: true });
    return () => c.removeEventListener("scroll", onScroll);
  }, [isEmpty, distanceFromBottom, follow]); // re-attach when the list appears

  // Content growth follows only in follow mode. Instant (not smooth): tokens
  // arrive many times a second and animated scrolls lag behind and fight the
  // scrollbar.
  useEffect(() => {
    if (stickRef.current) scrollToBottom(false);
  }, [messages, streamingText, isStreaming, scrollToBottom]);

  // A send always re-engages follow and jumps to the newest message — even
  // after the user had scrolled away from the previous run.
  useEffect(() => {
    if (scrollToBottomSignal) {
      follow(true);
      scrollToBottom();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- signal-only dep by design
  }, [scrollToBottomSignal]);

  // Any new run (send / regenerate / continue) starts in follow mode.
  const wasStreamingRef = useRef(false);
  useEffect(() => {
    if (isStreaming && !wasStreamingRef.current) {
      follow(true);
      scrollToBottom(false);
    }
    wasStreamingRef.current = Boolean(isStreaming);
  }, [isStreaming, follow, scrollToBottom]);

  // Opening / switching a conversation parks at its latest message.
  useEffect(() => {
    follow(true);
    const raf = window.requestAnimationFrame(() => scrollToBottom(false));
    return () => window.cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- id-only dep by design
  }, [conversationId]);

  const visibleAll = getVisibleMessages(messages, Boolean(isStreaming && streamingText !== undefined));

  // ---- Render window -------------------------------------------------------- #
  // Rendering thousands of MessageBubble+Markdown trees at once janks long
  // conversations (each bubble runs remark/highlight). We render the most
  // recent RENDER_WINDOW messages and offer a "load earlier" button that
  // extends the window; state resets on conversation switch.
  const RENDER_WINDOW = 60;
  const RENDER_STEP = 60;
  const [renderCount, setRenderCount] = useState(RENDER_WINDOW);
  useEffect(() => {
    setRenderCount(RENDER_WINDOW);
  }, [conversationId]);
  const hiddenCount = Math.max(0, visibleAll.length - renderCount);
  const shownMessages = hiddenCount > 0 ? visibleAll.slice(hiddenCount) : visibleAll;

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
    <div className={cn("relative flex min-h-0 flex-1 flex-col", className)}>
      <div ref={containerRef} className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-3xl px-4 py-6">
          {hiddenCount > 0 && (
            <div className="mb-4 flex justify-center">
              <button
                type="button"
                onClick={() => setRenderCount((c) => c + RENDER_STEP)}
                className="rounded-full border border-border bg-card px-4 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-accent"
              >
                加载更早的 {Math.min(hiddenCount, RENDER_STEP)} 条消息（还有 {hiddenCount} 条）
              </button>
            </div>
          )}
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

      {/* Jump-to-latest: shown while follow is off; pulsing dot while an
          answer is streaming in below the viewport. */}
      {!stuck && (
        <button
          type="button"
          aria-label="回到底部"
          onClick={() => {
            follow(true);
            scrollToBottom();
          }}
          className="absolute bottom-4 left-1/2 flex h-8 w-8 -translate-x-1/2 items-center justify-center rounded-full border border-border bg-background shadow-md transition-colors hover:bg-accent"
        >
          <ArrowDown className="h-4 w-4" />
          {isStreaming && (
            <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 animate-pulse rounded-full bg-blue-500" />
          )}
        </button>
      )}
    </div>
  );
}
