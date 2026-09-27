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
import { USER_MEMORIES_QUERY_KEY } from "@/lib/memories";
import { userErrorMessage } from "@/lib/api-error";
import {
  initialStreamState,
  rebuildLastSendFromMessages,
  reduce,
  type AssistantCommit,
  type StreamEvent,
  type StreamState,
} from "@/lib/chat-stream-state";
import {
  CONVERSATIONS_QUERY_KEY,
  CONVERSATION_DETAIL_QUERY_KEY,
} from "@/hooks/useConversations";
import type {
  AgentStep,
  ChatMention,
  Citation,
  FinishReason,
  GenerationStatus,
  Message,
  PendingApproval,
} from "@/lib/types";
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
  /** ``@``-references decoded from the sent text (extends knowledgeBaseIds). */
  mentions?: ChatMention[];
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
 *
 * 「事件 + 状态 → 新状态」的转移全在 `lib/chat-stream-state.ts` 的 `reduce` 里
 * （纯的、可单测）；本文件只采集事件、把结果写回 React，并执行留在浏览器侧的副
 * 作用（缓存失效、图 store、toast、取消 run）。
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

  // 当前这一轮状态核的入口，见 `createTurn`。
  const turnApplyRef = useRef<((event: StreamEvent) => void) | null>(null);

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
      const rebuilt = rebuildLastSendFromMessages(
        detail?.messages ?? [],
        conversationId
      );
      if (rebuilt) lastSendRef.current = rebuilt;
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
  // One streaming turn = a state-core `state` + SSE handlers that feed it.
  // Both the live send path (run) and the durable reattach path create one,
  // so a reattached run rebuilds identical state (text / steps / citations /
  // graph) and finalizes through the same commit logic.
  const createTurn = useCallback((seedConversationId: string | null) => {
    let state = initialStreamState(seedConversationId);
    // 「已经写进 React 的值」的镜像：只有变了的字段才 setState，调用点与渲染次数
    // 都等同于老实现逐个 setState 的写法。
    let published: StreamState | null = null;
    let shownText = "";
    let shownSources = state.citations;
    let appliedCommit: AssistantCommit | null = null;

    /**
     * 落库本轮的 assistant 消息（可能是半截）。内容与终态闸门都来自状态核的
     * `commit`，这里只补上 wall clock 与图 store 才能给出的两个字段。
     */
    const writeAssistantMessage = (commit: AssistantCommit) => {
      const isMulti = useAgentRunStore.getState().active.nodes.length >= 2;
      const msg: Message = {
        id: commit.messageId,
        conversation_id: commit.conversationId,
        role: "assistant",
        content: commit.content,
        metadata: {
          finish_reason: commit.finishReason,
          citations: commit.citations,
          steps: commit.steps,
          run_id: commit.runId || undefined,
          multi_agent: isMulti || undefined,
        },
        model_name: null,
        created_at: new Date().toISOString(),
      };
      appendMessage(commit.conversationId, msg);
      queryClient.invalidateQueries({
        queryKey: CONVERSATION_DETAIL_QUERY_KEY(commit.conversationId),
      });
      // The backend auto-titles a fresh conversation from this turn (cheap
      // truncation immediately, LLM refinement after the answer) — refetch
      // the sidebar list so the new title shows up without a manual reload.
      queryClient.invalidateQueries({
        queryKey: CONVERSATIONS_QUERY_KEY,
      });
      // Auto-proposed memory candidates are written at the end of the turn.
      // Refresh the chat affordance so they appear without a page reload.
      queryClient.invalidateQueries({ queryKey: USER_MEMORIES_QUERY_KEY });
      // 这一轮可能已经扣了积分。侧边栏余额与 /settings/credits 都从
      // ["credits"] 缓存读取（staleTime 30s），不主动失效就会在连续对话时
      // 一直显示轮前余额 —— 用户被挡时余额数字毫无变化，恰是体验最差的 surprises。
      queryClient.invalidateQueries({
        queryKey: ["credits"],
      });
    };

    const publish = () => {
      if (state.text !== shownText) {
        shownText = state.text;
        enqueueStreamingText(state.text);
      }
      if (state.citations !== shownSources) {
        shownSources = state.citations;
        // Mirror into the Context Panel so the Sources tab can render them.
        useContextPanelStore.getState().setSources(state.citations);
      }
      if (!published || published.conversationId !== state.conversationId) {
        setCurrentConversationId(state.conversationId);
      }
      // runId 一轮内只前进不清空（老实现也只在 run_started / runtime_selected /
      // 形状合法的 agent_graph 上写它）。
      if (state.runId && (!published || published.runId !== state.runId)) {
        setCurrentRunId(state.runId);
      }
      if (!published || published.citations !== state.citations) {
        setCitations(state.citations);
      }
      if (!published || published.steps !== state.steps) setSteps(state.steps);
      if (!published || published.stepsSinceTextFrom !== state.stepsSinceTextFrom) {
        setStepsSinceTextFrom(state.stepsSinceTextFrom);
      }
      if (!published || published.pendingApprovals !== state.pendingApprovals) {
        setPendingApprovals(state.pendingApprovals);
      }
      if (!published || published.error !== state.error) setError(state.error);
      if (!published || published.status !== state.status)
        setStatus(state.status);
      if (!published || published.finishReason !== state.finishReason)
        setFinishReason(state.finishReason);
      published = state;
      // 终态闸门在 reducer 里；这里的身份比较只防「同一个终态被重复 publish」。
      if (state.commit && state.commit !== appliedCommit) {
        appliedCommit = state.commit;
        writeAssistantMessage(state.commit);
      }
    };

    const apply = (event: StreamEvent) => {
      state = reduce(state, event);
      publish();
    };
    // 审批是在流之外被用户解掉的（REST 调用成功之后），但仍要改这一轮的状态，
    // 所以把当前轮的 `apply` 挂出去；否则 React 与状态核会各持一份审批列表。
    turnApplyRef.current = apply;

    const handlers: ChatStreamHandlers = {
      onMeta: (convId, msgId) => {
        apply({ kind: "meta", conversationId: convId, messageId: msgId });
        // Bump the conversation list so a new conversation appears.
        queryClient.invalidateQueries({ queryKey: CONVERSATIONS_QUERY_KEY });
      },
      onRunStarted: (e) => {
        apply({ kind: "run_started", runId: e.runId });
      },
      onRuntimeSelected: (e) => {
        apply({ kind: "runtime_selected", runId: e.runId });
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
          requestedMode: e.requestedMode,
          effectiveMode: e.effectiveMode,
        });
        if (e.multiAgentRequested && !e.multiAgentExecuted) {
          const reason = e.fallbackReason || "不可用";
          toast.warning("多 Agent 运行时当前不可用，本次已回退为普通模式", {
            description: `原因：${reason}。请检查服务器 .env 的 CREWAI_ENABLED 设置并重启后端（管理后台「Agent 运行时」诊断：/api/admin/agent-runtime）。`,
          });
        }
      },
      // ---- multi-agent graph events → store ----
      onAgentGraph: (e) => {
        const g = coerceGraph(e.runId, e.graph);
        if (!g) return;
        apply({ kind: "agent_graph", runId: e.runId });
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
            usage: e.usage,
            costUsd: e.costUsd,
            retrying: e.retrying,
          },
        });
      },
      // Rich step events: full stage output (expandable) + run heartbeat.
      // api.ts has already mapped snake_case → camelCase, and parse*() read the
      // backend field names off the original payload, so we dispatch the mapped
      // fields directly here.
      onStepOutput: (e) => {
        useAgentRunStore.getState().dispatch({
          type: "STEP_OUTPUT",
          runId: e.runId,
          agentId: e.agentId,
          text: e.text,
          truncated: e.truncated,
          chars: e.chars,
        });
      },
      onStepProgress: (e) => {
        useAgentRunStore.getState().dispatch({
          type: "STEP_PROGRESS",
          runId: e.runId,
          agentId: e.agentId,
          elapsedS: e.elapsedS,
          note: e.note,
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
        apply({ kind: "plan", summary: e.summary, steps: e.steps });
      },
      // Research-plan lifecycle surfaced as steps too (durable runs) — the two
      // frames carry the same payload, so they feed the same event.
      onResearchPlan: (e) => {
        apply({ kind: "plan", summary: e.summary, steps: e.steps });
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
        apply({ kind: "instruction_received", instruction: e.instruction });
      },
      onStepStarted: (e) => {
        apply({
          kind: "step_started",
          stepId: e.stepId,
          title: e.title,
          type: e.type,
        });
      },
      onStepCompleted: (e) => {
        apply({ kind: "step_completed", stepId: e.stepId, status: e.status });
      },
      onToken: (delta) => {
        apply({ kind: "token", delta });
      },
      onCitations: (cits) => {
        apply({ kind: "citations", citations: cits });
      },
      onToolCall: (e) => {
        apply({
          kind: "tool_call",
          id: e.id,
          name: e.name,
          arguments: e.arguments,
          dangerous: e.dangerous,
        });
        // Attribute the tool to its agent in the multi-agent graph store.
        if (e.agent_id) {
          useAgentRunStore.getState().dispatch({
            type: "TOOL_STARTED",
            runId: state.runId,
            agentId: e.agent_id,
            callId: e.id,
            name: e.name,
            title: (e.arguments?.query as string) || (e.arguments?.url as string),
          });
        }
      },
      onToolResult: (e) => {
        apply({
          kind: "tool_result",
          id: e.id,
          name: e.name,
          ok: e.ok,
          result: e.result,
        });
        if (e.agent_id) {
          useAgentRunStore.getState().dispatch({
            type: "TOOL_COMPLETED",
            runId: state.runId,
            agentId: e.agent_id,
            callId: e.id,
            ok: e.ok,
          });
        }
      },
      onApprovalRequired: (ap) => {
        apply({ kind: "approval_required", approval: ap });
      },
      onDone: (e) => {
        apply({
          kind: "done",
          messageId: e.messageId,
          finishReason: e.finishReason,
        });
      },
      onError: (e) => {
        apply({
          kind: "error",
          code: e.code,
          message: e.message,
          status: e.status,
          detail: e.detail,
        });
      },
    };
    // 把「轮初清空」也交给同一份状态：构造出一轮，React 里就只剩这一轮的状态。
    publish();

    return {
      handlers,
      apply,
      /** Pre-seed ids (reattach: the events log carries no meta frame). */
      seed(convId: string | null, msgId: string | null) {
        apply({ kind: "seed", conversationId: convId, messageId: msgId });
      },
      get state() {
        return state;
      },
    };
  }, [
    appendMessage,
    enqueueStreamingText,
    queryClient,
    setCitations,
    setCurrentConversationId,
    setCurrentRunId,
    setError,
    setFinishReason,
    setPendingApprovals,
    setStatus,
    setSteps,
    setStepsSinceTextFrom,
  ]);

  const run = useCallback(
    async (content: string, opts: SendOptions, isRegenerate: boolean) => {
      if (isStreaming) return;

      const controller = new AbortController();
      abortRef.current = controller;

      setIsStreaming(true);
      syncStreamingText("");
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
      const turn = createTurn(initialConversationId);

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
            // Inline @refs (知识库/文档/文件): scoping happens server-side.
            mentions: opts.mentions,
            content,
            regenerate: isRegenerate,
            // Phase 1: send the user-facing mode + bound attachments. The
            // backend IntentRouter derives the runtime/profile/tools.
            mode: opts.mode ?? "speed",
            attachmentIds: opts.attachmentIds,
            // B6: reasoning-effort hint (honored only by capable models).
            reasoningEffort: useChatUiStore.getState().reasoningEffort,
          }),
          turn.handlers,
          controller.signal
        );
      } catch (err) {
        if (!controller.signal.aborted) {
          // Genuine error (fetch failure, etc.). User aborts are handled in the
          // `finally` — parseSSEStream swallows AbortError so streamChat resolves
          // without throwing on a mid-stream Stop, and the cancel must still run.
          turn.apply({ kind: "fetch_failed", error: err });
        }
      } finally {
        const wasTerminated = turn.state.terminated;
        const userStop = userStopRef.current;
        turn.apply({
          kind: "stream_end",
          reason: controller.signal.aborted
            ? userStop
              ? "user_stop"
              : "unmount"
            : "socket_closed",
        });
        if (
          !wasTerminated &&
          controller.signal.aborted &&
          userStop &&
          turn.state.runId
        ) {
          // USER Stop（按钮）是唯一有权取消后端 run 的路径。
          api.cancelAgentRun(turn.state.runId).catch(() => undefined);
        }
        // 卸载式 abort 什么都不做：后端 durable run 继续跑，半截正文不入账，回到
        // 会话时由 reattach 整段重放补回视图。
        setIsStreaming(false);
        abortRef.current = null;
        userStopRef.current = false;
        syncStreamingText("");
      }
    },
    [
      isStreaming,
      currentConversationId,
      appendMessage,
      createTurn,
      syncStreamingText,
    ]
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
        const turn = createTurn(conversationId);
        // The durable event log has no meta frame — seed the ids up front.
        turn.seed(conversationId, active.messageId);
        setIsStreaming(true);
        syncStreamingText("");

        const resubscribe = () => reattachedRunsRef.current.delete(active.runId);
        try {
          await streamRunEvents(
            active.runId,
            {
              onEvent: (e) => {
                dispatchChatStreamEvent(
                  turn.handlers,
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
          if (!turn.state.terminated) {
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
    [isStreaming, createTurn, syncStreamingText]
  );

  // Resolve a pending approval; removes it from the list on success.
  const approveTool = useCallback(
    async (approvalId: string) => {
      const ap = pendingApprovals.find((p) => p.approvalId === approvalId);
      if (!ap) return;
      try {
        await api.approveToolCall(ap.runId, ap.approvalId);
        turnApplyRef.current?.({ kind: "approval_resolved", approvalId });
      } catch (err) {
        // 统一映射：401/409（审批已失效）等都各有其文案，不再一律"请重试"。
        setError(userErrorMessage(err));
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
        turnApplyRef.current?.({ kind: "approval_resolved", approvalId });
      } catch (err) {
        setError(userErrorMessage(err));
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
