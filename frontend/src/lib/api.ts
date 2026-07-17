"use client";

import {
  ChatRequest,
  Citation,
  Conversation,
  ConversationDetail,
  DocFile,
  KnowledgeBase,
  ModelConfig,
  ModelConfigInput,
  ModelTestResult,
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
  opts: { raw?: boolean; headers?: Record<string, string> } = {}
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
  listConversations: () => request<Conversation[]>("GET", "/api/conversations"),
  createConversation: (body: Partial<{ title: string; model_id: string | null; knowledge_base_id: string | null; system_prompt: string }> = {}) =>
    request<Conversation>("POST", "/api/conversations", body),
  getConversation: (id: string) => request<ConversationDetail>("GET", `/api/conversations/${id}`),
  updateConversation: (id: string, body: Partial<Conversation>) =>
    request<Conversation>("PATCH", `/api/conversations/${id}`, body),
  deleteConversation: (id: string) => request("DELETE", `/api/conversations/${id}`),

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
};

// ===========================================================================
// SSE chat streaming
// ===========================================================================
export interface ChatStreamHandlers {
  onMeta?: (conversationId: string, messageId: string) => void;
  onToken?: (delta: string) => void;
  onCitations?: (citations: Citation[]) => void;
  onToolCall?: (e: { id: string; name: string; arguments: Record<string, unknown> }) => void;
  onToolResult?: (e: { id: string; name: string; ok: boolean; result: unknown; error: string | null }) => void;
  onDone?: (e: { messageId: string; finishReason: string }) => void;
  onError?: (e: { code: string; message: string }) => void;
}

export async function streamChat(
  req: ChatRequest,
  handlers: ChatStreamHandlers,
  signal?: AbortSignal
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
    if (res.status === 401) {
      const ok = await refreshAccessToken();
      if (ok) return streamChat(req, handlers, signal);
    }
    handlers.onError?.({ code: "http_error", message });
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let curEvent = "message";

  const dispatch = (dataStr: string) => {
    if (!dataStr) return;
    try {
      const data = JSON.parse(dataStr);
      switch (curEvent) {
        case "meta":
          handlers.onMeta?.(data.conversation_id, data.message_id);
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
        case "done":
          handlers.onDone?.({ messageId: data.message_id, finishReason: data.finish_reason });
          break;
        case "error":
          handlers.onError?.(data);
          break;
      }
    } catch {
      /* malformed chunk, ignore */
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
}
