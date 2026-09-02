"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import {
  api,
  dispatchChatStreamEvent,
  findActiveConversationRun,
  streamChat,
  streamRunEvents,
  type ChatStreamHandlers,
} from "@/lib/api";
import { buildChatBody } from "@/lib/chat-request";
import { extractWebCitations, mergeCitations } from "@/lib/web-citations";
import {
  CONVERSATIONS_QUERY_KEY,
  CONVERSATION_DETAIL_QUERY_KEY,
} from "@/hooks/useConversations";
import type {
  AgentStep,
  Citation,
  FinishReason,
  GenerationStatus,
  Message,
  PendingApproval,
} from "@/lib/types";
import { finishReasonToStatus } from "@/lib/types";
import type { AgentEdgeStatus, AgentGraphNode } from "@/lib/agent-graph-types";
import { coerceGraph } from "@/hooks/useAgentRunGraph";
import { useAgentRunStore } from "@/stores/agent-run-store";
import { useChatUiStore } from "@/stores/chat-ui-store";
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
  /** Index into `steps` where the CURRENT tool batch begins — i.e. steps
   *  emitted after the last text token. Slicing `steps` by it yields the
   *  batch "sandwiched" between the narration so far and the next narration,
   *  which the UI shows; older batches are considered consumed and hidden. */
  stepsSinceTextFrom: number;
  /** Pending human-approval requests for dangerous tools in the live run. */
  pendingApprovals: PendingApproval[];
  currentConversationId: string | null;
  currentRunId: string | null;
  error: string | null;
  /** Terminal status of the last turn (complete/truncated/cancelled/error/interrupted). */
  status: GenerationStatus;
  /** Raw finish_reason of the last turn (null until the turn ends). */
  finishReason: FinishReason | null;
  /** Re-send the last user message to get a new assistant reply. */
  regenerate: () => Promise<void>;
  /** Continue a truncated/interrupted/cancelled answer (new turn, no repeat). */
  continueGeneration: () => Promise<void>;
  /** Approve a pending dangerous-tool call (resumes the run). */
  approveTool: (approvalId: string) => Promise<void>;
  /** Reject a pending dangerous-tool call (run continues without it). */
  rejectTool: (approvalId: string, reason?: string) => Promise<void>;
  /** Rebuild the replayable last-send state from persisted send_params. */
  rebuildLastSend: (conversationId: string | null) => void;
  /**
   * Adopt a still-running durable run for a conversation (browser refresh /
   * returning to a conversation whose run survives server-side). No-op when
   * the conversation has no active run or a stream is already attached.
   */
  reattach: (conversationId: string) => Promise<void>;
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
  const lastSendRef = useRef<{
    content: string;
    opts: SendOptions;
  } | null>(null);

  // Distinguishes a USER-initiated stop (cancel the backend run) from an
  // unmount cleanup abort (only close the SSE subscription — the durable
  // run keeps executing on the worker, and reattach picks it back up when
  // the user returns to the conversation).
  const userStopRef = useRef(false);

  // Close the in-flight SSE subscription when the consumer unmounts, so
  // navigating away mid-stream doesn't leak the connection. This does NOT
  // cancel the backend run — the durable worker keeps generating, and the
  // reattach path resumes the view on return.
  useEffect(() => {
    return () => {
      userStopRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  const [isStreaming, setIsStreaming] = useState(false);
  const [streamingText, setStreamingText] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [steps, setSteps] = useState<AgentStep[]>([]);
  // GPT-style sandwiched tool batches: the UI only shows steps emitted after
  // the last text token (see ChatStreamState.stepsSinceTextFrom).
  const [stepsSinceTextFrom, setStepsSinceTextFrom] = useState(0);
  const [pendingApprovals, setPendingApprovals] = useState<PendingApproval[]>([]);
  const [currentConversationId, setCurrentConversationId] = useState<
    string | null
  >(null);
  const [currentRunId, setCurrentRunId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<GenerationStatus>("complete");
  const [finishReason, setFinishReason] = useState<FinishReason | null>(null);

  // ---- Streaming token throttle ------------------------------------------- #
  // Each SSE token delta used to setState immediately, re-rendering the whole
  // markdown tree per delta (O(n²) parse cost on long answers). We accumulate
  // into a ref and flush to state at most once per animation frame — the screen
  // can't paint faster anyway, so this collapses dozens of renders per frame
  // into one without any visible latency.
  const pendingTextRef = useRef<string | null>(null);
  const flushHandleRef = useRef<number | null>(null);
  const flushStreamingText = useCallback(() => {
    flushHandleRef.current = null;
    if (pendingTextRef.current !== null) {
      setStreamingText(pendingTextRef.current);
      pendingTextRef.current = null;
    }
  }, []);
  const enqueueStreamingText = useCallback(
    (text: string) => {
      pendingTextRef.current = text;
      if (flushHandleRef.current === null) {
        flushHandleRef.current = requestAnimationFrame(flushStreamingText);
      }
    },
    [flushStreamingText],
  );
  const syncStreamingText = useCallback(
    (text: string) => {
      // Immediate (uncancelled) set + drop any scheduled flush.
      if (flushHandleRef.current !== null) {
        cancelAnimationFrame(flushHandleRef.current);
        flushHandleRef.current = null;
      }
      pendingTextRef.current = null;
      setStreamingText(text);
    },
    [],
  );

  // Remember the last send so regenerate() can replay it.
  // REBUILD AFTER REFRESH: the backend persists `send_params` on each user
  // message (mode/model/kb/attachments); when the in-memory ref is empty —
  // e.g. after a page reload — we reconstruct it from the newest user message
  // in the given conversation's detail cache, so regenerate()/continue()
  // never become silent no-ops across a reload.
  const rebuildLastSend = useCallback(
    (conversationId: string | null) => {
      if (!conversationId || lastSendRef.current) return;
      const detail = queryClient.getQueryData(
        CONVERSATION_DETAIL_QUERY_KEY(conversationId)
      ) as { messages?: Message[] } | undefined;
      const msgs = detail?.messages ?? [];
      for (let i = msgs.length - 1; i >= 0; i--) {
        const m = msgs[i];
        if (m.role !== "user") continue;
        const meta = m.metadata as
          | { send_params?: { mode?: string; model_id?: string | null; knowledge_base_ids?: string[]; attachment_ids?: string[] } }
          | undefined;
        const sp = meta?.send_params;
        lastSendRef.current = {
          content: m.content,
          opts: {
            conversationId,
            mode: (sp?.mode as SendOptions["mode"]) ?? undefined,
            modelId: sp?.model_id ?? null,
            knowledgeBaseIds: sp?.knowledge_base_ids,
            attachmentIds: sp?.attachment_ids,
          },
        };
        return;
      }
    },
    [queryClient]
  );

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

  // ---- Turn session factory ---------------------------------------------
  // One streaming turn = a bundle of mutable locals + SSE handlers. Both
  // the live send path (run) and the durable reattach path create one, so a
  // reattached run rebuilds identical state (text / steps / citations /
  // graph) and finalizes through the same commit logic.
  const createTurnSession = (seedConversationId: string | null) => {
    // Mutable locals tracked across stream events.
    let resolvedConversationId = seedConversationId;
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

    // Terminal-once guard: a single turn commits exactly one assistant
    // message, whether it ends via done, error, cancel, or a dropped socket.
    // Without this, onDone + the React Query refetch + a stray second done
    // could append duplicate assistant messages.
    let terminated = false;

    const errorFinish = (code?: string): FinishReason => {
      switch (code) {
        case "provider_timeout":
          return "timeout";
        case "stream_disconnected":
          return "stream_disconnected";
        case "provider_error":
          return "provider_error";
        default:
          return "error";
      }
    };

    /** Commit the (possibly partial) assistant message exactly once. */
    const commitAssistant = (
      convId: string,
      content: string,
      fr: FinishReason,
      msgId: string,
      extraMeta: Record<string, unknown> = {}
    ) => {
      if (terminated) return;
      terminated = true;
      const isMulti = useAgentRunStore.getState().active.nodes.length >= 2;
      const msg: Message = {
        id: msgId,
        conversation_id: convId,
        role: "assistant",
        content,
        metadata: {
          finish_reason: fr,
          citations: accumulatedCitations,
          steps: accumulatedStepsRef.current,
          run_id: resolvedRunId || undefined,
          multi_agent: isMulti || undefined,
          ...extraMeta,
        },
        model_name: null,
        created_at: new Date().toISOString(),
      };
      appendMessage(convId, msg);
      queryClient.invalidateQueries({
        queryKey: CONVERSATION_DETAIL_QUERY_KEY(convId),
      });
      // The backend auto-titles a fresh conversation from this turn (cheap
      // truncation immediately, LLM refinement after the answer) — refetch
      // the sidebar list so the new title shows up without a manual reload.
      queryClient.invalidateQueries({
        queryKey: CONVERSATIONS_QUERY_KEY,
      });
      setFinishReason(fr);
      setStatus(finishReasonToStatus(fr));
    };

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
      onRuntimeSelected: (e) => {
        resolvedRunId = e.runId;
        setCurrentRunId(e.runId);
        // Track the runtime selection on the agent-run store so the panel
        // can show runtime/profile and, crucially, a FALLBACK warning when a
        // multi-agent request couldn't run (never a silent single-model run).
        useAgentRunStore.getState().setRuntimeSelection({
          runId: e.runId,
          requestedRuntime: e.requestedRuntime,
          effectiveRuntime: e.effectiveRuntime,
          agentProfile: e.agentProfile,
          multiAgentRequested: e.multiAgentRequested,
          multiAgentExecuted: e.multiAgentExecuted,
          fallbackReason: e.fallbackReason,
          isDemo: e.isDemo,
        });
        if (e.multiAgentRequested && !e.multiAgentExecuted) {
          const reason = e.fallbackReason || "不可用";
          toast.warning("多 Agent 运行时当前不可用，本次已回退为普通模式", {
            description: `原因：${reason}。请在根目录 .env 启用 CREWAI_ENABLED=true 或 AGENT_DEMO_MODE=true 并重启后端。`,
          });
        }
      },
      // ---- multi-agent graph events → store ----
      onAgentGraph: (e) => {
        const g = coerceGraph(e.runId, e.graph);
        if (!g) return;
        resolvedRunId = e.runId;
        setCurrentRunId(e.runId);
        useAgentRunStore.getState().setActiveRun(e.runId);
        useAgentRunStore.getState().dispatch({ type: "GRAPH_INITIALIZED", runId: e.runId, graph: g });
        // Auto-open the Execution tab ONLY for genuine multi-agent crews
        // (≥2 nodes). Single-agent (native) turns surface via the trigger pill
        // + inline bubble status instead of a forced panel pop — opening the
        // panel on every plain message would be noisy. Respect a user close.
        if (
          g.nodes.length >= 2 &&
          !useContextPanelStore.getState().isSuppressed(e.runId)
        ) {
          useContextPanelStore.getState().openWith("execution");
        }
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
      // Research-plan lifecycle surfaced as steps too (durable runs).
      onResearchPlan: (e) => {
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
      onRunPaused: (e) => {
        useAgentRunStore.getState().dispatch({
          type: "RUN_STATUS",
          runId: e.runId,
          status: "waiting_approval",
        });
      },
      onRunResumed: (e) => {
        useAgentRunStore.getState().dispatch({
          type: "RUN_STATUS",
          runId: e.runId,
          status: "running",
        });
      },
      onRunInstructionReceived: (e) => {
        stepSeq += 1;
        const step: AgentStep = {
          id: `instr-${stepSeq}`,
          sequence: stepSeq,
          type: "approval",
          title: `已接收追加指引：${e.instruction}`,
          status: "done",
        };
        stepById.set(step.id, step);
        upsertStep(step);
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
        enqueueStreamingText(accumulated);
        // Text resuming consumes the batch before it: everything emitted so
        // far is now "narration history", and the next step starts a fresh
        // batch to be shown after this text.
        setStepsSinceTextFrom(accumulatedStepsRef.current.length);
      },
      onCitations: (cits) => {
        // Merge (not replace): KB document citations arrive early, web sources
        // arrive later via onToolResult — both must coexist in the Sources tab.
        accumulatedCitations = mergeCitations(accumulatedCitations, cits);
        setCitations(accumulatedCitations);
        // Mirror into the Context Panel so the Sources tab can render them.
        useContextPanelStore.getState().setSources(accumulatedCitations);
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
        // Promote real web tool output into verifiable Sources. web_search /
        // http_get results arrive here as a stringified-JSON payload; turning
        // them into web Citations makes the "来源" tab list the actual pages
        // and search hits the agent used (merged with any KB citations).
        if (e.ok && (e.name === "web_search" || e.name === "http_get")) {
          const web = extractWebCitations(e.name, e.result);
          if (web.length) {
            accumulatedCitations = mergeCitations(accumulatedCitations, web);
            setCitations(accumulatedCitations);
            useContextPanelStore.getState().setSources(accumulatedCitations);
          }
        }
      },
      onApprovalRequired: (ap) => {
        setPendingApprovals((prev) =>
          prev.some((p) => p.approvalId === ap.approvalId) ? prev : [...prev, ap]
        );
      },
      onDone: ({ messageId, finishReason: fr }) => {
        const finalId = messageId || assistantMessageId;
        if (resolvedConversationId) {
          commitAssistant(resolvedConversationId, accumulated, fr, finalId);
        } else {
          terminated = true;
          setFinishReason(fr);
          setStatus(finishReasonToStatus(fr));
        }
      },
      onError: ({ code, message }) => {
        const fr = errorFinish(code);
        setError(message);
        if (resolvedConversationId) {
          // Preserve whatever was streamed before the error.
          commitAssistant(resolvedConversationId, accumulated, fr, assistantMessageId);
        } else {
          terminated = true;
          setFinishReason(fr);
          setStatus(finishReasonToStatus(fr));
        }
      },
    };
    return {
      handlers,
      commitAssistant,
      /** Pre-seed ids (reattach: the events log carries no meta frame). */
      seed(convId: string | null, msgId: string | null) {
        if (convId) {
          resolvedConversationId = convId;
          setCurrentConversationId(convId);
        }
        if (msgId) assistantMessageId = msgId;
      },
      markTerminated() {
        terminated = true;
      },
      get resolvedConversationId() {
        return resolvedConversationId;
      },
      get assistantMessageId() {
        return assistantMessageId;
      },
      get resolvedRunId() {
        return resolvedRunId;
      },
      get accumulated() {
        return accumulated;
      },
      get terminated() {
        return terminated;
      },
    };
  };

  const run = useCallback(
    async (content: string, opts: SendOptions, isRegenerate: boolean) => {
      if (isStreaming) return;

      const controller = new AbortController();
      abortRef.current = controller;

      setIsStreaming(true);
      syncStreamingText("");
      setCitations([]);
      setSteps([]);
      setStepsSinceTextFrom(0);
      setPendingApprovals([]);
      setError(null);
      setStatus("complete");
      setFinishReason(null);
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
      // Mark the stream as belonging to this conversation IMMEDIATELY (not on
      // onMeta). The page gates the streaming bubble on
      // `currentConversationId === activeConversationId`; without this the
      // bubble stayed hidden until the backend's meta frame arrived, so the
      // user saw nothing (no sent-message echo, no streaming animation) for a
      // beat after pressing send.
      setCurrentConversationId(initialConversationId);

      const session = createTurnSession(initialConversationId);

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
            mode: opts.mode ?? "speed",
            attachmentIds: opts.attachmentIds,
            // B6: reasoning-effort hint (honored only by capable models).
            reasoningEffort: useChatUiStore.getState().reasoningEffort,
          }),
          session.handlers,
          controller.signal
        );
      } catch (err) {
        if (!controller.signal.aborted) {
          // Genuine error (fetch failure, etc.). User aborts are handled in the
          // `finally` — parseSSEStream swallows AbortError so streamChat resolves
          // without throwing on a mid-stream Stop, and the cancel must still run.
          const message = err instanceof Error ? err.message : "发生未知错误";
          setError(message);
          if (session.resolvedConversationId && session.accumulated) {
            session.commitAssistant(
              session.resolvedConversationId,
              session.accumulated,
              "error",
              session.assistantMessageId
            );
          } else {
            session.markTerminated();
            setFinishReason("error");
            setStatus("error");
          }
        }
      } finally {
        if (!session.terminated) {
          if (controller.signal.aborted) {
            if (userStopRef.current) {
              // USER Stop (button): preserve partial as cancelled and cancel
              // the backend run — the only path allowed to do so.
              if (session.resolvedConversationId && session.accumulated) {
                session.commitAssistant(
                  session.resolvedConversationId,
                  session.accumulated,
                  "cancelled",
                  session.assistantMessageId || `cancelled-${Date.now()}`
                );
              } else {
                session.markTerminated();
                setFinishReason("cancelled");
                setStatus("cancelled");
              }
              if (session.resolvedRunId) {
                api.cancelAgentRun(session.resolvedRunId).catch(() => undefined);
              }
            }
            // Unmount cleanup abort: do nothing here. The durable run keeps
            // executing on the worker; the partial stays uncommitted and the
            // reattach path (conversation remount) replays the full event
            // log, so returning to the page shows the finished answer.
          } else if (session.resolvedConversationId && session.accumulated) {
            // Socket dropped with NO terminal event and NO user abort.
            setError("连接中断，已保留已生成内容");
            session.commitAssistant(
              session.resolvedConversationId,
              session.accumulated,
              "stream_disconnected",
              session.assistantMessageId || `interrupted-${Date.now()}`
            );
          }
        }
        setIsStreaming(false);
        abortRef.current = null;
        userStopRef.current = false;
        syncStreamingText("");
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
    // User-initiated: the ONLY path allowed to cancel the backend run.
    userStopRef.current = true;
    abortRef.current?.abort();
  }, []);

  const regenerate = useCallback(async () => {
    const last = lastSendRef.current;
    if (!last) {
      // Older messages (pre send_params) can't be replayed — say so instead
      // of silently doing nothing.
      toast.error("无法重新生成：缺少该消息的原始发送参数");
      return;
    }
    await run(last.content, last.opts, true);
  }, [run]);

  // Continue a truncated/interrupted/cancelled answer. The partial assistant
  // text is already in the conversation history (committed), so a short user
  // turn lets the model resume. Kept concise + user-readable (it becomes a
  // normal user message bubble and is persisted like one — no internal
  // directive leaked into the transcript).
  const continueGeneration = useCallback(async () => {
    const last = lastSendRef.current;
    const convId = currentConversationId;
    if (!last || !convId) {
      toast.error("无法继续生成：缺少上一条消息的上下文");
      return;
    }
    await run("请继续上面的生成，不要重复已有内容。", { ...last.opts, conversationId: convId }, false);
  }, [run, currentConversationId]);

  // ---- Durable reattach ----------------------------------------------------
  // A run keeps executing on the worker when the SSE tail dies (refresh,
  // navigation, new conversation). On conversation open, adopt a non-terminal
  // run: replay its durable event log through the SAME turn-session handlers
  // so the live view (text / steps / citations) rebuilds and finishes through
  // the same commit path. The run itself is NEVER touched here.
  const reattachInFlightRef = useRef(false);
  const reattachedRunsRef = useRef<Set<string>>(new Set());
  const isStreamingRef = useRef(false);
  useEffect(() => {
    isStreamingRef.current = isStreaming;
  }, [isStreaming]);

  const reattach = useCallback(
    async (conversationId: string) => {
      if (isStreaming || isStreamingRef.current || reattachInFlightRef.current) return;
      reattachInFlightRef.current = true;
      try {
        const active = await findActiveConversationRun(conversationId);
        if (!active) return;
        if (reattachedRunsRef.current.has(active.runId)) return;
        // A send that raced in while we probed wins — never hijack its stream.
        if (isStreamingRef.current) return;
        reattachedRunsRef.current.add(active.runId);

        const controller = new AbortController();
        abortRef.current = controller;
        const session = createTurnSession(conversationId);
        // The durable event log has no meta frame — seed the ids up front.
        session.seed(conversationId, active.messageId);
        setIsStreaming(true);
        syncStreamingText("");
        setCitations([]);
        setSteps([]);
        setStepsSinceTextFrom(0);
        setPendingApprovals([]);
        setError(null);
        setFinishReason(null);
        setStatus("complete");

        const resubscribe = () => reattachedRunsRef.current.delete(active.runId);
        try {
          await streamRunEvents(
            active.runId,
            {
              onEvent: (e) => {
                dispatchChatStreamEvent(
                  session.handlers,
                  e.event_type,
                  JSON.stringify(e.data)
                );
              },
              // Network blip while the run is still going: forget the run so a
              // follow-up reattach (conversation switch / effect re-run) can
              // pick the stream back up from a fresh full replay.
              onDisconnect: resubscribe,
            },
            { signal: controller.signal }
          );
        } finally {
          // Closing the tail must never cancel the backend run — unlike the
          // inline path, an abort here only ends THIS subscription. The next
          // reattach replays from sequence 0 and rebuilds everything.
          if (!session.terminated) {
            resubscribe();
          }
          setIsStreaming(false);
          abortRef.current = null;
          syncStreamingText("");
        }
      } finally {
        reattachInFlightRef.current = false;
      }
    },
    // `createTurnSession` is a stable-shape factory re-created per render; its
    // identity changing just re-creates this callback, which is harmless.
    [isStreaming, createTurnSession]
  );

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
    stepsSinceTextFrom,
    pendingApprovals,
    currentConversationId,
    currentRunId,
    error,
    status,
    finishReason,
    regenerate,
    continueGeneration,
    approveTool,
    rejectTool,
    /** Rebuild the replayable last-send from persisted send_params (call on
     *  conversation load / refresh so regenerate & continue keep working). */
    rebuildLastSend,
    reattach,
  };
}
