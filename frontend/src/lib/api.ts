"use client";

import {
  AgentRun,
  AgentStep,
  ChatAttachment,
  ChatRequest,
  Citation,
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
  ResearchPlanStep,
  ToolInfo,
  User,
} from "./types";
import { getAccessToken, setAccessToken } from "./auth";

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
// SSE chat streaming
// ===========================================================================
export interface ChatStreamHandlers {
  onMeta?: (conversationId: string, messageId: string) => void;
  onRunStarted?: (e: { runId: string; runtime: string; conversationId: string; messageId: string }) => void;
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
  onDone?: (e: { messageId: string; finishReason: string }) => void;
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

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let curEvent = "message";

  let terminated = false;
  const dispatch = (dataStr: string) => {
    if (!dataStr) return;
    let data: any;
    try {
      data = JSON.parse(dataStr);
    } catch {
      return; // malformed JSON payload — drop this one event only
    }
    try {
      switch (curEvent) {
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
      console.error("[streamChat] handler error for event", curEvent, err);
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    let dataLines: string[] = [];
    for (const raw of lines) {
      const line = raw.trimEnd();
      if (line === "") {
        if (dataLines.length) dispatch(dataLines.join("\n"));
        dataLines = [];
        curEvent = "message";
        continue;
      }
      if (line.startsWith(":")) continue; // comment / keepalive
      if (line.startsWith("event:")) {
        curEvent = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      }
    }
  }
  if (buffer.trim()) dispatch(buffer.trim());
  // If the socket closed without a terminal done/error frame (proxy absolute
  // timeout, backend restart mid-run), surface it — otherwise the caller's
  // finally would silently erase the partial reply with no error shown.
  if (!terminated) {
    handlers.onError?.({ code: "stream_disconnected", message: "连接已中断，请重试" });
  }
}
