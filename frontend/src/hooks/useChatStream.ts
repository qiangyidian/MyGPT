"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";
import { streamChat, type ChatStreamHandlers } from "@/lib/api";
import {
  CONVERSATIONS_QUERY_KEY,
  CONVERSATION_DETAIL_QUERY_KEY,
} from "@/hooks/useConversations";
import type { Citation, Message, ResearchStep } from "@/lib/types";

export interface SendOptions {
  conversationId?: string | null;
  modelId?: string | null;
  knowledgeBaseId?: string | null;
  enableTools?: boolean;
}

export interface ChatStreamState {
  send: (content: string, opts?: SendOptions) => Promise<void>;
  stop: () => void;
  isStreaming: boolean;
  streamingText: string;
  citations: Citation[];
  /** Live agent steps (search/thinking) for the in-flight assistant reply. */
  steps: ResearchStep[];
  currentConversationId: string | null;
  error: string | null;
  /** Re-send the last user message to get a new assistant reply. */
  regenerate: () => Promise<void>;
}

/**
 * Drives the chat streaming experience.
 *
 * State flow:
 *  - `streamingText` accumulates token deltas for the live bubble.
 *  - `citations` holds the most recent RAG citations.
 *  - `currentConversationId` tracks the conversation being streamed into
 *    (the backend may create it on the fly; onMeta updates this).
 *  - An AbortController ref allows stop().
 *
 * On stream done, the final assistant message is appended to the
 * conversation detail cache so the message list shows it persistently,
 * and the streaming text is cleared.
 */
export function useChatStream(): ChatStreamState {
  const queryClient = useQueryClient();
  const abortRef = useRef<AbortController | null>(null);

  const [isStreaming, setIsStreaming] = useState(false);
  const [streamingText, setStreamingText] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [steps, setSteps] = useState<ResearchStep[]>([]);
  const [currentConversationId, setCurrentConversationId] = useState<
    string | null
  >(null);
  const [error, setError] = useState<string | null>(null);

  // Remember the last send so regenerate() can replay it.
  const lastSendRef = useRef<{
    content: string;
    opts: SendOptions;
  } | null>(null);

  /**
   * Append a message into the conversation detail cache.
   * If the conversation isn't yet cached, nothing happens — the next
   * refetch will pick it up.
   */
  const appendMessage = useCallback(
    (conversationId: string, msg: Message) => {
      queryClient.setQueryData(
        CONVERSATION_DETAIL_QUERY_KEY(conversationId),
        (old: unknown) => {
          if (!old || typeof old !== "object") return old;
          const detail = old as { messages?: Message[] };
          return { ...detail, messages: [...(detail.messages ?? []), msg] };
        }
      );
    },
    [queryClient]
  );

  const run = useCallback(
    async (content: string, opts: SendOptions, isRegenerate: boolean) => {
      if (isStreaming) return;

      const controller = new AbortController();
      abortRef.current = controller;

      setIsStreaming(true);
      setStreamingText("");
      setCitations([]);
      setSteps([]);
      setError(null);

      if (!isRegenerate) {
        lastSendRef.current = { content, opts };
      }

      const initialConversationId =
        opts.conversationId ?? currentConversationId ?? null;

      // Mutable locals tracked across stream events.
      let resolvedConversationId = initialConversationId;
      let assistantMessageId = "";
      // Refs so the onDone/onError closures can read the accumulated values
      // without depending on stale React state.
      let accumulated = "";
      let accumulatedCitations: Citation[] = [];
      let accumulatedSteps: ResearchStep[] = [];

      // Optimistically append the user's own message into the cache so it
      // appears instantly in the message list.
      if (initialConversationId && content && !isRegenerate) {
        const optimisticUser: Message = {
          id: `optimistic-user-${Date.now()}`,
          conversation_id: initialConversationId,
          role: "user",
          content,
          metadata: {},
          model_name: null,
          created_at: new Date().toISOString(),
        };
        appendMessage(initialConversationId, optimisticUser);
      }

      const handlers: ChatStreamHandlers = {
        onMeta: (convId, msgId) => {
          resolvedConversationId = convId;
          assistantMessageId = msgId;
          setCurrentConversationId(convId);
          // Bump the conversation list so a new conversation appears.
          queryClient.invalidateQueries({ queryKey: CONVERSATIONS_QUERY_KEY });
        },
        onToken: (delta) => {
          accumulated += delta;
          setStreamingText(accumulated);
        },
        onCitations: (cits) => {
          accumulatedCitations = cits;
          setCitations(cits);
        },
        onToolCall: (e) => {
          const step: ResearchStep = {
            id: e.id,
            name: e.name,
            arguments: e.arguments,
            status: "running",
          };
          accumulatedSteps = [...accumulatedSteps, step];
          setSteps(accumulatedSteps);
        },
        onToolResult: (e) => {
          accumulatedSteps = accumulatedSteps.map((s) =>
            s.id === e.id
              ? { ...s, status: e.ok ? "done" : "error", result: typeof e.result === "string" ? e.result : JSON.stringify(e.result) }
              : s
          );
          setSteps(accumulatedSteps);
        },
        onDone: ({ messageId, finishReason }) => {
          const finalId = messageId || assistantMessageId;
          if (resolvedConversationId) {
            const msg: Message = {
              id: finalId,
              conversation_id: resolvedConversationId,
              role: "assistant",
              content: accumulated,
              metadata: {
                finish_reason: finishReason,
                citations: accumulatedCitations,
                steps: accumulatedSteps,
              },
              model_name: null,
              created_at: new Date().toISOString(),
            };
            appendMessage(resolvedConversationId, msg);
            queryClient.invalidateQueries({
              queryKey: CONVERSATION_DETAIL_QUERY_KEY(resolvedConversationId),
            });
          }
        },
        onError: ({ message }) => {
          setError(message);
        },
      };

      try {
        await streamChat(
          {
            conversation_id: initialConversationId,
            model_id: opts.modelId ?? null,
            knowledge_base_id: opts.knowledgeBaseId ?? null,
            content,
            regenerate: isRegenerate,
            enable_tools: opts.enableTools,
          },
          handlers,
          controller.signal
        );
      } catch (err) {
        if (controller.signal.aborted) {
          // User cancelled — keep partial text as a frozen message if we can.
          if (resolvedConversationId && accumulated) {
            const msg: Message = {
              id: assistantMessageId || `aborted-${Date.now()}`,
              conversation_id: resolvedConversationId,
              role: "assistant",
              content: accumulated,
              metadata: { finish_reason: "aborted" },
              model_name: null,
              created_at: new Date().toISOString(),
            };
            appendMessage(resolvedConversationId, msg);
          }
        } else {
          const message =
            err instanceof Error ? err.message : "发生未知错误";
          setError(message);
        }
      } finally {
        setIsStreaming(false);
        abortRef.current = null;
        setStreamingText("");
      }
    },
    [isStreaming, currentConversationId, appendMessage, queryClient]
  );

  const send = useCallback(
    async (content: string, opts?: SendOptions) => {
      await run(content, opts ?? {}, false);
    },
    [run]
  );

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const regenerate = useCallback(async () => {
    const last = lastSendRef.current;
    if (!last) return;
    await run(last.content, last.opts, true);
  }, [run]);

  return {
    send,
    stop,
    isStreaming,
    streamingText,
    citations,
    steps,
    currentConversationId,
    error,
    regenerate,
  };
}
