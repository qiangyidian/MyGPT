"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";
import { api, streamChat, type ChatStreamHandlers } from "@/lib/api";
import {
  CONVERSATIONS_QUERY_KEY,
  CONVERSATION_DETAIL_QUERY_KEY,
} from "@/hooks/useConversations";
import type { AgentStep, Citation, Message, PendingApproval } from "@/lib/types";

export interface SendOptions {
  conversationId?: string | null;
  modelId?: string | null;
  knowledgeBaseId?: string | null;
  enableTools?: boolean;
  executionMode?: "auto" | "chat" | "agent";
  agentProfile?: string;
}

export interface ChatStreamState {
  send: (content: string, opts?: SendOptions) => Promise<void>;
  stop: () => void;
  isStreaming: boolean;
  streamingText: string;
  citations: Citation[];
  /** Live agent execution steps (plan / agent / tool / review / approval). */
  steps: AgentStep[];
  /** Pending human-approval requests for dangerous tools in the live run. */
  pendingApprovals: PendingApproval[];
  currentConversationId: string | null;
  currentRunId: string | null;
  error: string | null;
  /** Re-send the last user message to get a new assistant reply. */
  regenerate: () => Promise<void>;
  /** Approve a pending dangerous-tool call (resumes the run). */
  approveTool: (approvalId: string) => Promise<void>;
  /** Reject a pending dangerous-tool call (run continues without it). */
  rejectTool: (approvalId: string, reason?: string) => Promise<void>;
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
  const [steps, setSteps] = useState<AgentStep[]>([]);
  const [pendingApprovals, setPendingApprovals] = useState<PendingApproval[]>([]);
  const [currentConversationId, setCurrentConversationId] = useState<
    string | null
  >(null);
  const [currentRunId, setCurrentRunId] = useState<string | null>(null);
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
      setPendingApprovals([]);
      setError(null);

      if (!isRegenerate) {
        lastSendRef.current = { content, opts };
      }

      const initialConversationId =
        opts.conversationId ?? currentConversationId ?? null;

      // Mutable locals tracked across stream events.
      let resolvedConversationId = initialConversationId;
      let assistantMessageId = "";
      let resolvedRunId = "";
      // Refs so the onDone/onError closures can read the accumulated values
      // without depending on stale React state.
      let accumulated = "";
      let accumulatedCitations: Citation[] = [];
      let stepSeq = 0;
      const stepById = new Map<string, AgentStep>();

      const upsertStep = (step: AgentStep) => {
        const next = [...stepById.values()];
        // preserve insertion order by sequence
        next.sort((a, b) => a.sequence - b.sequence);
        accumulatedStepsRef.current = next;
        setSteps(next);
      };
      const accumulatedStepsRef = { current: [] as AgentStep[] };

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
        onRunStarted: (e) => {
          resolvedRunId = e.runId;
          setCurrentRunId(e.runId);
        },
        onPlanCreated: (e) => {
          e.steps.forEach((p) => {
            stepSeq += 1;
            stepById.set(p.id, {
              id: p.id,
              sequence: stepSeq,
              type: "plan",
              title: p.title,
              summary: p.id === e.steps[0]?.id ? e.summary : undefined,
              status: "pending",
            });
          });
          upsertStep(stepById.get(e.steps[0]?.id ?? "") as AgentStep);
        },
        onStepStarted: (e) => {
          // If the step id was already announced (plan), flip to running;
          // otherwise create a new step of the given type.
          const existing = stepById.get(e.stepId);
          stepSeq += 1;
          const step: AgentStep = existing
            ? { ...existing, status: "running", type: (e.type as AgentStep["type"]) || existing.type, title: e.title }
            : {
                id: e.stepId,
                sequence: stepSeq,
                type: (e.type as AgentStep["type"]) || "agent",
                title: e.title,
                status: "running",
                startedAt: new Date().toISOString(),
              };
          stepById.set(e.stepId, step);
          upsertStep(step);
        },
        onStepCompleted: (e) => {
          const existing = stepById.get(e.stepId);
          if (existing) {
            stepById.set(e.stepId, {
              ...existing,
              status: (e.status as AgentStep["status"]) || "done",
              finishedAt: new Date().toISOString(),
            });
            upsertStep(stepById.get(e.stepId) as AgentStep);
          }
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
          stepSeq += 1;
          const step: AgentStep = {
            id: e.id,
            sequence: stepSeq,
            type: "tool",
            title: e.name,
            status: "running",
            startedAt: new Date().toISOString(),
            tool: {
              name: e.name,
              dangerous: e.dangerous,
              argumentsPreview: e.arguments,
            },
          };
          stepById.set(e.id, step);
          upsertStep(step);
        },
        onToolResult: (e) => {
          const existing = stepById.get(e.id);
          if (existing) {
            const resultPreview =
              typeof e.result === "string"
                ? e.result
                : e.result != null
                  ? JSON.stringify(e.result)
                  : undefined;
            stepById.set(e.id, {
              ...existing,
              status: e.ok ? "done" : "error",
              finishedAt: new Date().toISOString(),
              tool: {
                ...(existing.tool ?? { name: e.name }),
                name: e.name,
                ok: e.ok,
                resultPreview,
              },
            });
            upsertStep(stepById.get(e.id) as AgentStep);
          }
        },
        onApprovalRequired: (ap) => {
          setPendingApprovals((prev) =>
            prev.some((p) => p.approvalId === ap.approvalId) ? prev : [...prev, ap]
          );
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
                steps: accumulatedStepsRef.current,
                run_id: resolvedRunId || undefined,
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
            execution_mode: opts.executionMode ?? "auto",
            agent_profile: opts.agentProfile ?? "general",
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
          // Also cancel the backend run if we know its id.
          if (resolvedRunId) {
            api.cancelAgentRun(resolvedRunId).catch(() => undefined);
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

  // Resolve a pending approval; removes it from the list on success.
  const approveTool = useCallback(
    async (approvalId: string) => {
      const ap = pendingApprovals.find((p) => p.approvalId === approvalId);
      if (!ap) return;
      try {
        await api.approveToolCall(ap.runId, ap.approvalId);
        setPendingApprovals((prev) => prev.filter((p) => p.approvalId !== approvalId));
      } catch {
        // surface as a generic error
        setError("确认失败，请重试");
      }
    },
    [pendingApprovals]
  );

  const rejectTool = useCallback(
    async (approvalId: string, reason?: string) => {
      const ap = pendingApprovals.find((p) => p.approvalId === approvalId);
      if (!ap) return;
      try {
        await api.rejectToolCall(ap.runId, ap.approvalId, reason);
        setPendingApprovals((prev) => prev.filter((p) => p.approvalId !== approvalId));
      } catch {
        setError("拒绝失败，请重试");
      }
    },
    [pendingApprovals]
  );

  return {
    send,
    stop,
    isStreaming,
    streamingText,
    citations,
    steps,
    pendingApprovals,
    currentConversationId,
    currentRunId,
    error,
    regenerate,
    approveTool,
    rejectTool,
  };
}
