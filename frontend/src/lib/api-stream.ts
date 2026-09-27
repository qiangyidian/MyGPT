"use client";

import { API_BASE, request, refreshAccessToken } from "./api-client";
import { getAccessToken } from "./auth";
import { parseSSEStream } from "./sse-parser";
import type {
  ChatRequest,
  Citation,
  FinishReason,
  PendingApproval,
  ResearchPlanStep,
} from "./types";

// ===========================================================================
// SSE chat streaming
// ===========================================================================
export interface ChatStreamHandlers {
  onMeta?: (conversationId: string, messageId: string) => void;
  onRunStarted?: (e: { runId: string; runtime: string; conversationId: string; messageId: string }) => void;
  onRuntimeSelected?: (e: {
    runId: string;
    requestedMode: string;
    effectiveMode: string;
    requestedRuntime: string;
    effectiveRuntime: string;
    agentProfile: string;
    multiAgentRequested: boolean;
    multiAgentExecuted: boolean;
    fallbackReason: string | null;
    isDemo: boolean;
  }) => void;
  onPlanCreated?: (e: { summary: string; steps: { id: string; title: string }[] }) => void;
  onStepStarted?: (e: { stepId: string; title: string; type: string; agent?: string }) => void;
  onStepCompleted?: (e: { stepId: string; status: string }) => void;
  onAgentGraph?: (e: { runId: string; graph: unknown }) => void;
  onAgentStatus?: (e: {
    runId: string; agentId: string; status: string; taskTitle?: string;
    startedAt?: string; finishedAt?: string; durationMs?: number;
    outputSummary?: string; error?: string;
    usage?: Record<string, number>; costUsd?: number;
    retrying?: { attempt: number; error: string };
  }) => void;
  onStepOutput?: (e: {
    runId: string; agentId: string; text: string; truncated: boolean; chars: number;
  }) => void;
  onStepProgress?: (e: {
    runId: string; agentId: string; elapsedS: number; note?: string;
  }) => void;
  onAgentEdge?: (e: { runId: string; edgeId: string; status: string; label?: string }) => void;
  onRunStatus?: (e: { runId: string; status: string; currentAgentIds?: string[] }) => void;
  onToken?: (delta: string) => void;
  onCitations?: (citations: Citation[]) => void;
  onToolCall?: (e: { id: string; name: string; arguments: Record<string, unknown>; dangerous?: boolean; approval_id?: string; agent_id?: string; task_id?: string }) => void;
  onToolResult?: (e: { id: string; name: string; ok: boolean; result: unknown; error: string | null; agent_id?: string; task_id?: string }) => void;
  onApprovalRequired?: (e: PendingApproval) => void;
  onResearchPlan?: (e: {
    runId: string;
    status: string;
    summary: string;
    steps: ResearchPlanStep[];
    requiresConfirmation: boolean;
    updated: boolean;
  }) => void;
  onRunInstructionReceived?: (e: { runId: string; instruction: string; acknowledged: boolean }) => void;
  onRunPaused?: (e: { runId: string; reason: string; pausedAt?: string }) => void;
  onRunResumed?: (e: { runId: string; resumedAt?: string }) => void;
  onDone?: (e: { messageId: string; finishReason: FinishReason }) => void;
  onError?: (e: {
    code: string;
    message: string;
    /** HTTP status when the failure came from a rejected response. */
    status?: number;
    /** Raw server body (may hold a pydantic error array) — for `toUserError`. */
    detail?: unknown;
  }) => void;
}

/**
 * Dispatch one chat SSE event to the handlers. Returns true when the event is
 * terminal (done/error) so the caller can stop consuming the stream.
 *
 * Exported (not private to streamChat) so the durable reattach path can feed
 * events from the agent-runs event log through the EXACT same mapping — the
 * worker persists AgentEvent.kind, which IS the SSE event name.
 */
export function dispatchChatStreamEvent(
  handlers: ChatStreamHandlers,
  eventName: string,
  dataStr: string
): boolean {
  if (!dataStr) return false;
  let data: any;
  try {
    data = JSON.parse(dataStr);
  } catch {
    return false; // malformed JSON payload — drop this one event only
  }
  try {
    switch (eventName) {
        case "meta":
          handlers.onMeta?.(data.conversation_id, data.message_id);
          break;
        case "run_started":
          handlers.onRunStarted?.({
            runId: data.run_id,
            runtime: data.runtime,
            conversationId: data.conversation_id,
            messageId: data.message_id,
          });
          break;
        case "runtime_selected":
          handlers.onRuntimeSelected?.({
            runId: data.run_id,
            requestedMode: data.requested_mode,
            effectiveMode: data.effective_mode,
            requestedRuntime: data.requested_runtime,
            effectiveRuntime: data.effective_runtime,
            agentProfile: data.agent_profile,
            multiAgentRequested: !!data.multi_agent_requested,
            multiAgentExecuted: !!data.multi_agent_executed,
            fallbackReason: data.fallback_reason ?? null,
            isDemo: !!data.is_demo,
          });
          break;
        case "plan_created":
          handlers.onPlanCreated?.({ summary: data.summary, steps: data.steps ?? [] });
          break;
        case "step_started":
          handlers.onStepStarted?.({
            stepId: data.step_id,
            title: data.title,
            type: data.type,
            agent: data.agent,
          });
          break;
        case "step_completed":
          handlers.onStepCompleted?.({ stepId: data.step_id, status: data.status });
          break;
        case "agent_graph":
          handlers.onAgentGraph?.({ runId: data.run_id, graph: data.graph });
          break;
        case "agent_status":
          handlers.onAgentStatus?.({
            runId: data.run_id,
            agentId: data.agent_id,
            status: data.status,
            taskTitle: data.task_title,
            startedAt: data.started_at,
            finishedAt: data.finished_at,
            durationMs: data.duration_ms,
            outputSummary: data.output_summary,
            error: data.error,
            usage: data.usage,
            costUsd: data.cost_usd,
            retrying: data.retrying,
          });
          break;
        case "step_output":
          handlers.onStepOutput?.({
            runId: data.run_id,
            agentId: data.agent_id,
            text: data.text,
            truncated: data.truncated,
            chars: data.chars,
          });
          break;
        case "step_progress":
          handlers.onStepProgress?.({
            runId: data.run_id,
            agentId: data.agent_id,
            elapsedS: data.elapsed_s,
            note: data.note,
          });
          break;
        case "agent_edge":
          handlers.onAgentEdge?.({ runId: data.run_id, edgeId: data.edge_id, status: data.status, label: data.label });
          break;
        case "run_status":
          handlers.onRunStatus?.({ runId: data.run_id, status: data.status, currentAgentIds: data.current_agent_ids });
          break;
        case "token":
          handlers.onToken?.(data.delta ?? "");
          break;
        case "citations":
          handlers.onCitations?.(data.citations);
          break;
        case "tool_call":
          handlers.onToolCall?.(data);
          break;
        case "tool_result":
          handlers.onToolResult?.(data);
          break;
        case "approval_required":
          handlers.onApprovalRequired?.({
            runId: data.run_id,
            approvalId: data.approval_id,
            toolName: data.tool_name,
            summary: data.summary,
            riskLevel: data.risk_level,
            argumentsPreview: data.arguments_preview ?? {},
          });
          break;
        case "research_plan":
          handlers.onResearchPlan?.({
            runId: data.run_id,
            status: data.status,
            summary: data.summary,
            steps: data.steps ?? [],
            requiresConfirmation: data.requires_confirmation,
            updated: false,
          });
          break;
        case "research_plan_updated":
          handlers.onResearchPlan?.({
            runId: data.run_id,
            status: data.status,
            summary: data.summary,
            steps: data.steps ?? [],
            requiresConfirmation: data.requires_confirmation,
            updated: true,
          });
          break;
        case "run_instruction_received":
          handlers.onRunInstructionReceived?.({
            runId: data.run_id,
            instruction: data.instruction,
            acknowledged: data.acknowledged,
          });
          break;
        case "run_paused":
          handlers.onRunPaused?.({
            runId: data.run_id,
            reason: data.reason,
            pausedAt: data.paused_at,
          });
          break;
        case "run_resumed":
          handlers.onRunResumed?.({ runId: data.run_id, resumedAt: data.resumed_at });
          break;
        case "done":
          handlers.onDone?.({ messageId: data.message_id, finishReason: data.finish_reason });
          break;
        case "error":
          handlers.onError?.(data);
          break;
      }
  } catch (err) {
    // Surface handler bugs to the console instead of silently masking them as
    // a "malformed chunk"; the stream continues past a single bad event.
    console.error("[streamChat] handler error for event", eventName, err);
  }
  return eventName === "done" || eventName === "error";
}

export interface ActiveConversationRun {
  runId: string;
  messageId: string | null;
  status: string;
}

/**
 * Latest non-terminal (pending/running) durable run for a conversation — the
 * reattach probe on conversation open / browser refresh. Runs survive client
 * disconnects when BACKGROUND_WORKER is enabled server-side.
 */
export async function findActiveConversationRun(
  conversationId: string
): Promise<ActiveConversationRun | null> {
  try {
    const runs = await request<
      Array<{ id: string; message_id: string | null; status: string }>
    >("GET", `/api/agent-runs?conversation_id=${encodeURIComponent(conversationId)}`);
    const active = runs.find((r) => r.status === "running" || r.status === "pending");
    if (!active) return null;
    return { runId: active.id, messageId: active.message_id, status: active.status };
  } catch {
    return null;
  }
}

export async function streamChat(
  req: ChatRequest,
  handlers: ChatStreamHandlers,
  signal?: AbortSignal,
  /** internal: bounds the 401 → refresh → retry path to a single attempt. */
  _attempt = 0
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/chat/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}),
    },
    credentials: "include",
    body: JSON.stringify(req),
    signal,
  });

  if (!res.ok || !res.body) {
    let message = res.statusText;
    let detail: unknown;
    try {
      const d = await res.json();
      detail = d;
      message =
        typeof d.message === "string" && d.message
          ? d.message
          : typeof d.detail === "string" && d.detail
            ? d.detail
            : message;
    } catch { /* ignore */ }
    // Retry once after a refresh; never recurse unbounded (a refresh that
    // returns ok while the endpoint still 401s would otherwise stack-overflow).
    if (res.status === 401 && _attempt < 1) {
      const ok = await refreshAccessToken();
      if (ok) return streamChat(req, handlers, signal, _attempt + 1);
    }
    // `status` + `detail` let the caller run this through `toUserError` (the
    // server's own Chinese wins; otherwise the status decides).
    handlers.onError?.({
      code: "http_error",
      message,
      status: res.status,
      ...(detail === undefined ? {} : { detail }),
    });
    return;
  }

  let terminated = false;
  const dispatch = (eventName: string, dataStr: string) => {
    if (dispatchChatStreamEvent(handlers, eventName, dataStr)) terminated = true;
  };

  // Robust SSE framing: the parser keeps its buffer/event/data accumulators
  // ACROSS network chunks (the old code re-declared `dataLines` inside the read
  // loop, so any event split across two chunks was silently dropped — the cause
  // of random missing tokens / lost `done`). Abort returns cleanly (not a throw).
  for await (const frame of parseSSEStream(res.body, signal)) {
    dispatch(frame.event || "message", frame.data);
    if (terminated) break;
  }
  // If the socket closed without a terminal done/error frame (and the user did
  // NOT abort), surface a disconnect — otherwise the caller's finally would
  // silently erase the partial reply with no error shown.
  if (!terminated && !signal?.aborted) {
    handlers.onError?.({ code: "stream_disconnected", message: "连接已中断，请重试" });
  }
}

// ===========================================================================
// Durable run-event SSE (Task 12)
//   GET /api/agent-runs/{run_id}/events — cursor-replay SSE.
//   READ-ONLY: never executes or cancels the run. A client disconnect closes
//   only this subscription; the run keeps running on the worker. The frame's
//   `id:` line carries the event sequence, echoed back as `Last-Event-ID` on
//   reconnect so replay resumes exactly where it left off.
// ===========================================================================
export interface RunEventStreamHandlers {
  /**
   * Called for each durable event frame. `sequence` comes from the SSE `id:`
   * line (falls back to `data.sequence` when the line is absent). `event_type`
   * is the `event:` field; `data` is the parsed JSON payload.
   */
  onEvent?: (e: {
    runId: string;
    sequence: number;
    event_type: string;
    data: Record<string, unknown>;
    id?: string;
  }) => void;
  /** Called whenever the cursor advances (the highest sequence seen so far). */
  onCursor?: (cursor: number) => void;
  /** Network drop (not a user abort) — the caller decides whether to reconnect. */
  onDisconnect?: () => void;
  /** Non-recoverable HTTP error (after the single 401 refresh retry). */
  onError?: (e: {
    code: string;
    message: string;
    /** HTTP status when the failure came from a rejected response. */
    status?: number;
    /** Raw server body (may hold a pydantic error array) — for `toUserError`. */
    detail?: unknown;
  }) => void;
}

export async function streamRunEvents(
  runId: string,
  handlers: RunEventStreamHandlers,
  opts: { signal?: AbortSignal; lastEventId?: number } = {},
  /** internal: bounds the 401 → refresh → retry path to a single attempt. */
  _attempt = 0,
): Promise<void> {
  const headers: Record<string, string> = {};
  if (getAccessToken()) headers["Authorization"] = `Bearer ${getAccessToken()}`;
  // Last-Event-ID seeds the cursor so the server replays only events past it.
  if (opts.lastEventId && opts.lastEventId > 0) {
    headers["Last-Event-ID"] = String(opts.lastEventId);
  }

  try {
    const res = await fetch(`${API_BASE}/api/agent-runs/${runId}/events`, {
      method: "GET",
      headers,
      credentials: "include",
      signal: opts.signal,
    });

    if (!res.ok || !res.body) {
      let message = res.statusText;
      let detail: unknown;
      try {
        const d = await res.json();
        detail = d;
        message =
          typeof d.message === "string" && d.message
            ? d.message
            : typeof d.detail === "string" && d.detail
              ? d.detail
              : message;
      } catch {
        /* ignore */
      }
      if (res.status === 401 && _attempt < 1) {
        const ok = await refreshAccessToken();
        if (ok) return streamRunEvents(runId, handlers, opts, _attempt + 1);
      }
      handlers.onError?.({
        code: "http_error",
        message,
        status: res.status,
        ...(detail === undefined ? {} : { detail }),
      });
      return;
    }

    let cursor = opts.lastEventId ?? 0;
    for await (const frame of parseSSEStream(res.body, opts.signal)) {
      if (!frame.data) continue;
      let data: Record<string, unknown>;
      try {
        data = JSON.parse(frame.data);
      } catch {
        continue; // malformed JSON — drop this one frame only
      }
      // Prefer the SSE `id:` line (the durable sequence); fall back to a
      // `sequence` field in the payload for streams that don't stamp `id:`.
      let sequence = -1;
      if (frame.id !== undefined && /^\d+$/.test(frame.id)) {
        sequence = parseInt(frame.id, 10);
      } else if (typeof data.sequence === "number") {
        sequence = data.sequence;
      }
      if (sequence > cursor) cursor = sequence;
      handlers.onEvent?.({
        runId,
        sequence,
        event_type: frame.event || "message",
        data,
        ...(frame.id !== undefined ? { id: frame.id } : {}),
      });
      handlers.onCursor?.(cursor);
    }
    // Socket ended without an abort → network drop / server-side close. Signal
    // the caller so it can decide to reconnect from the persisted cursor.
    if (!opts.signal?.aborted) {
      handlers.onDisconnect?.();
    }
  } catch (err) {
    // Intentional cancellation: the caller aborted the AbortController
    // (component unmount / runId switch / explicit clear). fetch() and the
    // stream iterator then reject with an AbortError — that's not a real
    // error, so swallow it silently (no onError, no onDisconnect). Anything
    // else is a genuine network failure: surface it so the caller reconnects.
    const aborted =
      opts.signal?.aborted === true ||
      (err instanceof DOMException && err.name === "AbortError");
    if (aborted) return;
    handlers.onError?.({
      code: "network_error",
      message: err instanceof Error ? err.message : String(err),
    });
  }
}
