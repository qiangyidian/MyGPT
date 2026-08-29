"use client";

import {
  AgentRun,
  AgentStep,
  ArtifactMeta,
  ChatAttachment,
  ChatRequest,
  Citation,
  Connector,
  ConnectorCreateInput,
  ConnectorUpdateInput,
  Conversation,
  ConversationDetail,
  DocFile,
  KnowledgeBase,
  MessageFeedback,
  MessageFeedbackRating,
  ModelConfig,
  ModelConfigInput,
  ModelTestResult,
  PendingApproval,
  Project,
  ProjectInput,
  ProviderManifest,
  ResearchPlanStep,
  RunActionResult,
  ToolInfo,
  User,
  UserMemory,
  UserMemoryEditInput,
  UserMemoryProposeInput,
} from "./types";
import { getAccessToken, setAccessToken } from "./auth";
import { parseSSEStream } from "./sse-parser";
import type { FinishReason } from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

let refreshing: Promise<boolean> | null = null;

async function refreshAccessToken(): Promise<boolean> {
  if (refreshing) return refreshing;
  refreshing = (async () => {
    try {
      const res = await fetch(`${API_BASE}/api/auth/refresh`, {
        method: "POST",
        credentials: "include",
        // Bound the refresh so a hung backend can't lock up every concurrent
        // 401 retry — `refreshing` is a shared singleton (see below).
        signal: AbortSignal.timeout(15_000),
      });
      if (!res.ok) return false;
      const data = await res.json();
      setAccessToken(data.access_token);
      return true;
    } catch {
      return false;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  opts: { raw?: boolean; headers?: Record<string, string>; signal?: AbortSignal } = {}
): Promise<T> {
  const doFetch = async (token: string | null): Promise<Response> => {
    const headers: Record<string, string> = { ...(opts.headers || {}) };
    if (body !== undefined && !(body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
    }
    if (token) headers["Authorization"] = `Bearer ${token}`;
    return fetch(`${API_BASE}${path}`, {
      method,
      headers,
      credentials: "include",
      // Forward an optional caller signal so long-lived calls can be cancelled
      // (a hung backend otherwise leaves the promise pending until the browser's
      // own ~300s network timeout).
      signal: opts.signal,
      body:
        body === undefined
          ? undefined
          : body instanceof FormData
            ? body
            : JSON.stringify(body),
    });
  };

  let res = await doFetch(getAccessToken());

  if (res.status === 401) {
    const ok = await refreshAccessToken();
    if (ok) res = await doFetch(getAccessToken());
    else {
      setAccessToken(null);
      throw new ApiError(401, "unauthorized", "会话已过期，请重新登录");
    }
  }

  if (!res.ok) {
    let code = "error";
    let message = res.statusText;
    try {
      const data = await res.json();
      code = data.code || code;
      message = data.message || data.detail || message;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, code, message);
  }
  if (opts.raw) return res as unknown as T;
  if (res.status === 204) return undefined as unknown as T;
  return (await res.json()) as T;
}

// ===========================================================================
// Auth
// ===========================================================================
export const api = {
  async register(email: string, username: string, password: string) {
    return request<{ user: User }>("POST", "/api/auth/register", {
      email,
      username,
      password,
    });
  },
  async login(email: string, password: string) {
    const data = await request<{
      access_token: string;
      expires_in: number;
      user: User;
    }>("POST", "/api/auth/login", { email, password });
    setAccessToken(data.access_token);
    return data;
  },
  async me() {
    return request<User>("GET", "/api/auth/me");
  },
  async logout() {
    try {
      await request("POST", "/api/auth/logout");
    } finally {
      setAccessToken(null);
    }
  },

  // ---- Conversations ----
  listConversations: (opts?: { q?: string; archived?: boolean; limit?: number; offset?: number }) => {
    const params = new URLSearchParams();
    if (opts?.q) params.set("q", opts.q);
    if (opts?.archived) params.set("archived", "true");
    if (opts?.limit) params.set("limit", String(opts.limit));
    if (opts?.offset) params.set("offset", String(opts.offset));
    const qs = params.toString();
    return request<Conversation[]>("GET", qs ? `/api/conversations?${qs}` : "/api/conversations");
  },
  createConversation: (body: Partial<{ title: string; model_id: string | null; knowledge_base_id: string | null; system_prompt: string }> = {}) =>
    request<Conversation>("POST", "/api/conversations", body),
  getConversation: (id: string) => request<ConversationDetail>("GET", `/api/conversations/${id}`),
  updateConversation: (
    id: string,
    body: Partial<{
      title: string;
      model_id: string | null;
      knowledge_base_id: string | null;
      system_prompt: string;
      pinned: boolean;
      archived: boolean;
    }>
  ) => request<Conversation>("PATCH", `/api/conversations/${id}`, body),
  deleteConversation: (id: string) => request("DELETE", `/api/conversations/${id}`),
  branchConversation: (conversationId: string, messageId: string, newContent: string) =>
    request<ConversationDetail>("POST", `/api/conversations/${conversationId}/branch`, {
      message_id: messageId,
      new_content: newContent,
    }),
  /** Parent + child branches of a conversation (branch tree navigation). */
  listConversationBranches: (conversationId: string) =>
    request<{ parent: Conversation | null; children: Conversation[] }>(
      "GET",
      `/api/conversations/${conversationId}/branches`,
    ),

  // ---- Chat attachments ----
  uploadChatAttachment: (conversationId: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("conversation_id", conversationId);
    return request<ChatAttachment>("POST", "/api/chat-attachments", fd);
  },
  listChatAttachments: (conversationId: string) =>
    request<ChatAttachment[]>(
      "GET",
      `/api/chat-attachments?conversation_id=${encodeURIComponent(conversationId)}`
    ),
  getChatAttachment: (id: string) => request<ChatAttachment>("GET", `/api/chat-attachments/${id}`),
  deleteChatAttachment: (id: string) => request("DELETE", `/api/chat-attachments/${id}`),
  saveAttachmentToKb: (id: string, knowledgeBaseId: string) =>
    request<ChatAttachment>("POST", `/api/chat-attachments/${id}/save-to-kb`, {
      knowledge_base_id: knowledgeBaseId,
    }),
  /** Fetch attachment bytes as a Blob (authenticated). Use for previews/downloads. */
  downloadAttachment: async (id: string): Promise<Blob> => {
    // Route through the central request() so an expired access token is
    // refreshed + the call retried (a raw fetch would just 401 after expiry).
    const res = await request<Response>(
      "GET",
      `/api/chat-attachments/${id}/content`,
      undefined,
      { raw: true }
    );
    return res.blob();
  },

  // ---- Message feedback ----
  setFeedback: (
    messageId: string,
    rating: MessageFeedbackRating,
    extra?: { reason?: string; comment?: string }
  ) =>
    request<MessageFeedback>("POST", `/api/messages/${messageId}/feedback`, {
      rating,
      reason: extra?.reason,
      comment: extra?.comment,
    }),
  deleteFeedback: (messageId: string) =>
    request("DELETE", `/api/messages/${messageId}/feedback`),
  getFeedback: (messageId: string) =>
    request<MessageFeedback | null>("GET", `/api/messages/${messageId}/feedback`),

  // ---- Models ----
  listModels: () => request<ModelConfig[]>("GET", "/api/models"),
  createModel: (body: ModelConfigInput) => request<ModelConfig>("POST", "/api/models", body),
  updateModel: (id: string, body: Partial<ModelConfigInput>) =>
    request<ModelConfig>("PUT", `/api/models/${id}`, body),
  deleteModel: (id: string) => request("DELETE", `/api/models/${id}`),
  testModel: (id: string) => request<ModelTestResult>("POST", `/api/models/${id}/test`),

  // ---- Knowledge bases ----
  listKnowledgeBases: () => request<KnowledgeBase[]>("GET", "/api/knowledge-bases"),
  createKnowledgeBase: (body: { name: string; description?: string; embedding_model_id?: string | null }) =>
    request<KnowledgeBase>("POST", "/api/knowledge-bases", body),
  getKnowledgeBase: (id: string) => request<KnowledgeBase>("GET", `/api/knowledge-bases/${id}`),
  deleteKnowledgeBase: (id: string) => request("DELETE", `/api/knowledge-bases/${id}`),
  listDocuments: (kbId: string) => request<DocFile[]>("GET", `/api/knowledge-bases/${kbId}/documents`),
  uploadDocument: (kbId: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<DocFile>("POST", `/api/knowledge-bases/${kbId}/documents`, fd);
  },
  deleteDocument: (id: string) => request("DELETE", `/api/documents/${id}`),
  reindexDocument: (id: string) => request<{ document_id: string; status: string; chunk_count: number }>("POST", `/api/documents/${id}/reindex`),

  // ---- Retrieval ----
  searchKnowledgeBase: (kbId: string, query: string, topK = 5) =>
    request<{ context: string; citations: Citation[] }>("POST", "/api/retrieval/search", {
      knowledge_base_id: kbId,
      query,
      top_k: topK,
    }),

  // ---- Tools ----
  listTools: () => request<ToolInfo[]>("GET", "/api/tools"),
  testTool: (name: string, args: Record<string, unknown>) =>
    request<{ ok: boolean; result: unknown; error: string | null }>("POST", "/api/tools/test", { name, arguments: args }),

  // ---- Admin ----
  adminListUsers: () => request<User[]>("GET", "/api/admin/users"),
  adminUpdateUser: (id: string, body: { role?: string; is_active?: boolean }) =>
    request<User>("PATCH", `/api/admin/users/${id}`, body),
  adminStats: () =>
    request<{ usage: unknown[]; status: unknown }>("GET", "/api/admin/stats"),
  /** Most recent audit events (tool calls, approvals, auth). */
  adminAuditLog: (limit = 200) =>
    request<
      Array<{
        id: string;
        actor_id: string | null;
        action: string;
        target: string | null;
        detail: Record<string, unknown> | null;
        created_at: string | null;
      }>
    >("GET", `/api/admin/audit?limit=${limit}`),

  // ---- Agent runs (Phase 3) ----
  listAgentRuns: (conversationId?: string) =>
    request<AgentRun[]>(
      "GET",
      conversationId ? `/api/agent-runs?conversation_id=${conversationId}` : "/api/agent-runs"
    ),
  getAgentRun: (runId: string) => request<AgentRun>("GET", `/api/agent-runs/${runId}`),
  approveToolCall: (runId: string, approvalId: string) =>
    request<{ ok: boolean; status: string; message: string | null }>(
      "POST",
      `/api/agent-runs/${runId}/approve`,
      { approval_id: approvalId }
    ),
  rejectToolCall: (runId: string, approvalId: string, reason?: string) =>
    request<{ ok: boolean; status: string; message: string | null }>(
      "POST",
      `/api/agent-runs/${runId}/reject`,
      { approval_id: approvalId, reason: reason ?? null }
    ),
  cancelAgentRun: (runId: string) =>
    request<{ ok: boolean; status: string; message: string | null }>(
      "POST",
      `/api/agent-runs/${runId}/cancel`
    ),

  // ---- Durable run controls (Task 12: pause/resume/cancel/instruction/plan) ----
  pauseAgentRun: (runId: string) =>
    request<RunActionResult>("POST", `/api/agent-runs/${runId}/pause`),
  resumeAgentRun: (runId: string) =>
    request<RunActionResult>("POST", `/api/agent-runs/${runId}/resume`),
  appendRunInstruction: (runId: string, instruction: string) =>
    request<RunActionResult>("POST", `/api/agent-runs/${runId}/instructions`, {
      instruction,
    }),
  confirmPlan: (runId: string) =>
    request<RunActionResult>("POST", `/api/agent-runs/${runId}/plan/confirm`),
  updatePlan: (
    runId: string,
    body: {
      summary?: string;
      steps?: Array<{ id: string; title: string; description?: string; sources?: string[] }>;
    },
  ) => request<RunActionResult>("POST", `/api/agent-runs/${runId}/plan/update`, body),
};

// ===========================================================================
// Projects (Phase 3)
// ===========================================================================
export const projectsApi = {
  list: () => request<Project[]>("GET", "/api/projects"),
  create: (body: ProjectInput) => request<Project>("POST", "/api/projects", body),
  update: (id: string, body: Partial<ProjectInput>) =>
    request<Project>("PATCH", `/api/projects/${id}`, body),
  delete: (id: string) => request("DELETE", `/api/projects/${id}`),
  assignConversation: (projectId: string, conversationId: string) =>
    request<Conversation>("POST", `/api/projects/${projectId}/conversations/${conversationId}`),
  unassignConversation: (projectId: string, conversationId: string) =>
    request<Conversation>("DELETE", `/api/projects/${projectId}/conversations/${conversationId}`),
};

// ===========================================================================
// Artifacts (Task 12) — tenant-scoped upload + authorized streaming download.
// ===========================================================================
export const artifactsApi = {
  list: () => request<ArtifactMeta[]>("GET", "/api/artifacts"),
  create: (
    file: File,
    fields: { source?: string; run_id?: string } = {},
  ): Promise<ArtifactMeta> => {
    const fd = new FormData();
    fd.append("file", file);
    if (fields.source) fd.append("source", fields.source);
    if (fields.run_id) fd.append("run_id", fields.run_id);
    return request<ArtifactMeta>("POST", "/api/artifacts", fd);
  },
  /** Download artifact bytes as a Blob (authenticated; owner/admin only). */
  download: async (id: string): Promise<Blob> => {
    const res = await request<Response>(
      "GET",
      `/api/artifacts/${id}`,
      undefined,
      { raw: true },
    );
    return res.blob();
  },
  /** Lightweight metadata (filename/size/media type) — no bytes transferred. */
  getMeta: (id: string) =>
    request<ArtifactMeta>("GET", `/api/artifacts/${id}/meta`),
  delete: (id: string) => request("DELETE", `/api/artifacts/${id}`),
};

// ===========================================================================
// User memories (Task 12) — opt-in cross-conversation semantic memory.
// ===========================================================================
export const memoriesApi = {
  list: () => request<UserMemory[]>("GET", "/api/memories"),
  propose: (body: UserMemoryProposeInput) =>
    request<UserMemory>("POST", "/api/memories", body),
  /** Bulk activate/deactivate all memories (active=false disables the feature). */
  bulkSet: (active: boolean) =>
    request<{ activated?: number; deactivated?: number }>(
      "POST",
      "/api/memories/bulk",
      { active },
    ),
  activate: (id: string) =>
    request<UserMemory>("POST", `/api/memories/${id}/activate`),
  deactivate: (id: string) =>
    request<UserMemory>("POST", `/api/memories/${id}/deactivate`),
  edit: (id: string, body: UserMemoryEditInput) =>
    request<UserMemory>("PATCH", `/api/memories/${id}`, body),
  delete: (id: string) => request("DELETE", `/api/memories/${id}`),
};

// ===========================================================================
// Connectors (Task 12) — tenant-scoped, audited credential management.
// ===========================================================================
export const connectorsApi = {
  listProviders: () =>
    request<ProviderManifest[]>("GET", "/api/connectors/providers"),
  list: () => request<Connector[]>("GET", "/api/connectors"),
  create: (body: ConnectorCreateInput) =>
    request<Connector>("POST", "/api/connectors", body),
  update: (id: string, body: ConnectorUpdateInput) =>
    request<Connector>("PATCH", `/api/connectors/${id}`, body),
  rotate: (id: string, credentials: Record<string, unknown>) =>
    request<Connector>("POST", `/api/connectors/${id}/rotate`, { credentials }),
  activate: (id: string) =>
    request<Connector>("POST", `/api/connectors/${id}/activate`),
  deactivate: (id: string) =>
    request<Connector>("POST", `/api/connectors/${id}/deactivate`),
  delete: (id: string) => request("DELETE", `/api/connectors/${id}`),
};

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
  onError?: (e: { code: string; message: string }) => void;
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
    try {
      const d = await res.json();
      message = d.message || d.detail || message;
    } catch { /* ignore */ }
    // Retry once after a refresh; never recurse unbounded (a refresh that
    // returns ok while the endpoint still 401s would otherwise stack-overflow).
    if (res.status === 401 && _attempt < 1) {
      const ok = await refreshAccessToken();
      if (ok) return streamChat(req, handlers, signal, _attempt + 1);
    }
    handlers.onError?.({ code: "http_error", message });
    return;
  }

  let terminated = false;
  const dispatch = (eventName: string, dataStr: string) => {
    if (!dataStr) return;
    let data: any;
    try {
      data = JSON.parse(dataStr);
    } catch {
      return; // malformed JSON payload — drop this one event only
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
          terminated = true;
          handlers.onDone?.({ messageId: data.message_id, finishReason: data.finish_reason });
          break;
        case "error":
          terminated = true;
          handlers.onError?.(data);
          break;
      }
    } catch (err) {
      // Surface handler bugs to the console instead of silently masking them as
      // a "malformed chunk"; the stream continues past a single bad event.
      console.error("[streamChat] handler error for event", eventName, err);
    }
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
  onError?: (e: { code: string; message: string }) => void;
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
      try {
        const d = await res.json();
        message = d.message || d.detail || message;
      } catch {
        /* ignore */
      }
      if (res.status === 401 && _attempt < 1) {
        const ok = await refreshAccessToken();
        if (ok) return streamRunEvents(runId, handlers, opts, _attempt + 1);
      }
      handlers.onError?.({ code: "http_error", message });
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
