"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, streamChat, type ChatStreamHandlers } from "@/lib/api";
import { buildChatBody } from "@/lib/chat-request";
import {
  CONVERSATIONS_QUERY_KEY,
  CONVERSATION_DETAIL_QUERY_KEY,
} from "@/hooks/useConversations";
import type { AgentStep, Citation, Message, PendingApproval } from "@/lib/types";
import type { AgentEdgeStatus, AgentGraphNode } from "@/lib/agent-graph-types";
import { coerceGraph } from "@/hooks/useAgentRunGraph";
import { useAgentRunStore } from "@/stores/agent-run-store";
import { useContextPanelStore } from "@/stores/context-panel-store";
import { useAttachmentStore } from "@/stores/attachment-store";
import type { UserChatMode } from "@/lib/types";

export interface SendOptions {
  conversationId?: string | null;
  modelId?: string | null;
  knowledgeBaseId?: string | null;
  /** Per-turn multi-KB selection. */
  knowledgeBaseIds?: string[];
  /** User-facing capability mode (Phase 1). */
  mode?: UserChatMode;
  /** Attachment ids to bind to the outgoing user message. */
  attachmentIds?: string[];
  /** Legacy/advanced overrides — not used by the new UI. */
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

  // Abort any in-flight stream when the consumer unmounts, so navigating away
  // mid-stream doesn't leak the SSE connection and let the backend run to
  // completion.
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

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
      // Reset the multi-agent graph store for a new turn (a new agent_graph
      // event will repopulate it; this also clears any dismissal so the panel
      // can auto-open for the new run).
      const store = useAgentRunStore.getState();
      store.resetActive();
      // New task clears the Context Panel suppression + sources from prior turn.
      useContextPanelStore.getState().resetForNewTask();

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
        // Snapshot the composer's attachment drafts onto the optimistic message
        // so attachment cards render immediately; the backend-provided metadata
        // replaces them once the turn reloads.
        const draftAttachments = useAttachmentStore
          .getState()
          .getDrafts(initialConversationId)
          .map((d) => ({
            id: d.id,
            filename: d.filename,
            mime_type: d.mime_type,
            size_bytes: d.size_bytes,
            status: d.status,
            parse_status: d.parse_status,
          }));
        const optimisticUser: Message = {
          id: `optimistic-user-${Date.now()}`,
          conversation_id: initialConversationId,
          role: "user",
          content,
          metadata: draftAttachments.length ? { attachments: draftAttachments } : {},
          model_name: null,
          created_at: new Date().toISOString(),
        };
        appendMessage(initialConversationId, optimisticUser);
        // Drafts are now bound to the outgoing message — clear the composer tray.
        useAttachmentStore.getState().clearDrafts(initialConversationId);
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
        // ---- multi-agent graph events → store ----
        onAgentGraph: (e) => {
          const g = coerceGraph(e.runId, e.graph);
          if (!g) return;
          resolvedRunId = e.runId;
          setCurrentRunId(e.runId);
          useAgentRunStore.getState().setActiveRun(e.runId);
          useAgentRunStore.getState().dispatch({ type: "GRAPH_INITIALIZED", runId: e.runId, graph: g });
        },
        onAgentStatus: (e) => {
          useAgentRunStore.getState().dispatch({
            type: "AGENT_STATUS",
            runId: e.runId,
            agentId: e.agentId,
            patch: {
              status: e.status as AgentGraphNode["status"],
              taskTitle: e.taskTitle,
              startedAt: e.startedAt,
              finishedAt: e.finishedAt,
              durationMs: e.durationMs,
              outputSummary: e.outputSummary,
              error: e.error,
            },
          });
        },
        onAgentEdge: (e) => {
          useAgentRunStore.getState().dispatch({
            type: "EDGE_STATUS",
            runId: e.runId,
            edgeId: e.edgeId,
            status: e.status as AgentEdgeStatus,
            label: e.label,
          });
        },
        onRunStatus: (e) => {
          useAgentRunStore.getState().dispatch({
            type: "RUN_STATUS",
            runId: e.runId,
            status: e.status as never,
          });
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
          // Mirror into the Context Panel so the Sources tab can render them.
          useContextPanelStore.getState().setSources(cits);
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
          // Attribute the tool to its agent in the multi-agent graph store.
          if (e.agent_id) {
            useAgentRunStore.getState().dispatch({
              type: "TOOL_STARTED",
              runId: resolvedRunId,
              agentId: e.agent_id,
              callId: e.id,
              name: e.name,
              title: (e.arguments?.query as string) || (e.arguments?.url as string),
            });
          }
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
          if (e.agent_id) {
            useAgentRunStore.getState().dispatch({
              type: "TOOL_COMPLETED",
              runId: resolvedRunId,
              agentId: e.agent_id,
              callId: e.id,
              ok: e.ok,
            });
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
            // multi_agent is true only if an agent_graph event arrived this turn.
            const isMulti = useAgentRunStore.getState().active.nodes.length >= 2;
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
                multi_agent: isMulti || undefined,
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
          buildChatBody({
            conversationId: initialConversationId,
            modelId: opts.modelId,
            knowledgeBaseId: opts.knowledgeBaseId,
            knowledgeBaseIds: opts.knowledgeBaseIds,
            content,
            regenerate: isRegenerate,
            // Phase 1: send the user-facing mode + bound attachments. The
            // backend IntentRouter derives the runtime/profile/tools.
            mode: opts.mode ?? "auto",
            attachmentIds: opts.attachmentIds,
          }),
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
