"use client";

import { request } from "./api-client";
export { API_BASE, ApiError } from "./api-client";
export {
  dispatchChatStreamEvent,
  findActiveConversationRun,
  streamChat,
  streamRunEvents,
} from "./api-stream";
export type {
  ActiveConversationRun,
  ChatStreamHandlers,
  RunEventStreamHandlers,
} from "./api-stream";

import {
  AdminAgentRunPage,
  AdminRunCommandRow,
  AdminRunEventPage,
  AdminRunSummary,
  AgentRun,
  AgentStep,
  ArtifactMeta,
  AttachmentTextPreview,
  ChatAttachment,
  ChatRequest,
  Citation,
  Connector,
  ConnectorCreateInput,
  ConnectorUpdateInput,
  Conversation,
  ConversationDetail,
  CreditAccountInfo,
  CreditAccountRow,
  CreditLedgerPage,
  DocFile,
  DocumentPreview,
  FeatureFlagPage,
  KnowledgeBase,
  MentionList,
  Message,
  MessageFeedback,
  MessageFeedbackRating,
  ModelConfig,
  ModelConfigInput,
  ModelTestResult,
  Project,
  ProjectImpact,
  ProjectPatch,
  ReindexResult,
  RetrievalSettings,
  ProjectInput,
  ProviderManifest,
  RedeemBatchCreateResult,
  RedeemBatchProgress,
  RedeemCodeActionResult,
  RedeemCodePage,
  RedeemResult,
  RunActionResult,
  ToolInfo,
  UploadCapabilities,
  User,
  UserMemory,
  UserMemoryEditInput,
  UserMemoryProposeInput,
  WechatBinding,
  WechatLoginInfo,
} from "./types";
import { setAccessToken } from "./auth";
import type { SpeechCapabilities, SpeechTranscribeResult } from "./types";
import type {
  MessageTruncateResult,
  MessageVersionDTO,
  PromptScope,
  PromptTemplate,
  PromptTemplateInput,
  VersionActivateResult,
} from "./types";
import type { RedeemBatchQuery } from "./redeem-batch";

export interface PageParams {
  limit?: number;
  offset?: number;
}

/**
 * Append ``limit`` / ``offset`` to a list URL, omitting what isn't set so a
 * bare call keeps hitting the backend's default first page.
 */
function withPageParams(path: string, page?: PageParams): string {
  if (!page) return path;
  const qs = new URLSearchParams();
  if (page.limit != null) qs.set("limit", String(page.limit));
  if (page.offset != null) qs.set("offset", String(page.offset));
  const q = qs.toString();
  return q ? `${path}?${q}` : path;
}

// --------------------------------------------------------------------------- //
// 管理端：用量报表 + 审计列表（对应 backend/app/schemas/admin.py 的 DTO）
// --------------------------------------------------------------------------- //

/** 一组用量求和项。``requests`` 是 assistant 行数：token 与成本只记在这些行上。 */
export interface AdminUsageMetrics {
  messages: number;
  user_messages: number;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
}

export interface AdminUsageRow extends AdminUsageMetrics {
  /** 分组键：按天是日期，按模型是模型名（NULL 时为空串），按用户是用户 id。 */
  key: string;
  /** 给人看的分组名；模型缺名时后端给「未记录模型」，不会是空白。 */
  label: string;
  email: string | null;
  username: string | null;
}

/** 取值与后端白名单同源：`admin_service.USAGE_GROUP_BY`（`backend/app/services/admin_service.py:30`）。 */
export type AdminUsageGroupBy = "day" | "model" | "user";

export interface AdminUsageQuery {
  /** UTC 日历日 `YYYY-MM-DD`，含当天；不传时后端补最近 30 天（`admin_service.py:34`）。 */
  start?: string;
  end?: string;
  groupBy?: AdminUsageGroupBy;
  /** 上限 500 = `admin_service.USAGE_MAX_LIMIT`（`admin_service.py:36`）。 */
  limit?: number;
  offset?: number;
}

export interface AdminUsagePage {
  /** 服务端补齐后实际生效的区间（含两端）。 */
  start: string;
  end: string;
  group_by: AdminUsageGroupBy;
  items: AdminUsageRow[];
  /** 分组总数（不是消息数），给翻页用。 */
  total: number;
  limit: number;
  offset: number;
  /** 整个区间的合计，与这一页装了哪几行无关。 */
  totals: AdminUsageMetrics;
}

export interface AdminAuditRow {
  id: string;
  actor_id: string | null;
  actor_email: string | null;
  actor_username: string | null;
  action: string;
  target: string | null;
  detail: Record<string, unknown> | null;
  created_at: string | null;
}

export interface AdminAuditQuery {
  action?: string;
  /** 前缀匹配，如 `credits:`。 */
  actionPrefix?: string;
  /** 用户 id（精确）或邮箱 / 用户名（模糊）。 */
  actor?: string;
  /** 关键字：匹配 target。 */
  q?: string;
  /** UTC 日历日 `YYYY-MM-DD`，含当天。 */
  start?: string;
  end?: string;
  /** 上限 500 = `AUDIT_MAX_LIMIT`（`backend/app/api/admin.py:34`）。 */
  limit?: number;
  offset?: number;
}

export interface AdminAuditPage {
  items: AdminAuditRow[];
  total: number;
  limit: number;
  offset: number;
}

// ===========================================================================
// Auth
// ===========================================================================
export const api = {
  async register(
    email: string,
    username: string,
    password: string,
    verificationCode: string
  ) {
    return request<{ user: User }>("POST", "/api/auth/register", {
      email,
      username,
      password,
      verification_code: verificationCode,
    });
  },
  /** Send a one-time registration code to the email. */
  async requestEmailCode(email: string) {
    return request<{ sent: boolean; debug_code?: string }>(
      "POST",
      "/api/auth/email-code",
      { email }
    );
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

  // ---- WeChat Official Account scan login (公众号验证码登录) ----
  /**
   * Public login-page hints. Unauthenticated, so it must not 401 the caller.
   * `configured` is false when the deployment has no QR image set — the panel
   * then shows a text hint instead of a broken image.
   */
  async fetchWechatLoginInfo() {
    const res = await request<{ data: WechatLoginInfo }>(
      "GET",
      "/api/wechat/login-info"
    );
    return res.data;
  },
  /** Redeem the code the Official Account sent, for a session. */
  async loginWithWechatCode(wechatCode: string) {
    const data = await request<{
      access_token: string;
      expires_in: number;
      user: User;
    }>("POST", "/api/auth/login/wechat", { wechat_code: wechatCode });
    setAccessToken(data.access_token);
    return data;
  },
  async fetchWechatBinding() {
    return request<WechatBinding>("GET", "/api/auth/wechat/binding");
  },
  /** Attach the WeChat that produced `wechatCode` to the account already logged in. */
  async bindWechat(wechatCode: string) {
    return request<WechatBinding>("POST", "/api/auth/wechat/binding", {
      wechat_code: wechatCode,
    });
  },
  async unbindWechat() {
    return request<WechatBinding>("DELETE", "/api/auth/wechat/binding");
  },
  /** 账号注销：需要密码二次确认；成功后服务端清除内容并使所有 token 失效。 */
  async deleteMyAccount(password: string) {
    await request<void>("DELETE", "/api/auth/me", { password });
    setAccessToken(null);
  },

  async logout() {
    try {
      await request("POST", "/api/auth/logout");
    } finally {
      setAccessToken(null);
    }
  },

  // ---- 密码：修改 / 找回 ----
  /**
   * 改密（需登录）。成功后服务端会 bump token_version，旧 access/refresh 全部
   * 失效，并当场下发新会话 —— 所以这里必须换掉本地 token，否则下一次请求就
   * 拿着一个已被拉黑的令牌去撞 401。密码只走请求体，绝不进 query string。
   */
  async changePassword(oldPassword: string | null, newPassword: string) {
    const data = await request<{
      access_token: string;
      expires_in: number;
      user: User;
    }>("POST", "/api/auth/password", {
      old_password: oldPassword || null,
      new_password: newPassword,
    });
    setAccessToken(data.access_token);
    return data;
  },
  /** 找回密码第一步：发重置验证码。邮箱是否存在都返回同一句话（防枚举）。 */
  async requestPasswordReset(email: string) {
    return request<{ ok: boolean; message: string }>(
      "POST",
      "/api/auth/password/forgot",
      { email }
    );
  },
  /** 找回密码第二步：凭验证码设置新密码（不需要登录，也不会建立会话）。 */
  async resetPasswordWithEmailCode(email: string, code: string, newPassword: string) {
    return request<{ ok: boolean; message: string }>("POST", "/api/auth/password/reset", {
      email,
      code,
      new_password: newPassword,
    });
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
      // `null` clears the override so the conversation falls back to the
      // platform default — the PATCH handler distinguishes "omitted" from
      // "sent as null", so widening this to null is what 恢复默认 needs.
      system_prompt: string | null;
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
  deleteChatAttachment: (id: string) => request("DELETE", `/api/chat-attachments/${id}`),
  /** Parsed-text preview (document content) for the preview dialog. */
  getAttachmentText: (id: string, maxChars = 20000) =>
    request<AttachmentTextPreview>(
      "GET",
      `/api/chat-attachments/${id}/text?max_chars=${maxChars}`
    ),
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

  /**
   * 导出整段会话（Markdown / JSON）。走 raw 是因为文件名在响应头里，
   * 而 central request() 会把 body 直接 JSON 解析掉。
   */
  exportConversation: async (
    id: string,
    format: "markdown" | "json"
  ): Promise<{ blob: Blob; contentDisposition: string | null }> => {
    const res = await request<Response>(
      "GET",
      `/api/conversations/${id}/export?format=${format}`,
      undefined,
      { raw: true }
    );
    return { blob: await res.blob(), contentDisposition: res.headers.get("content-disposition") };
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
  // Both list endpoints are paginated server-side (``limit`` defaults to the
  // backend's page size, so calling them with no args returns the first page).
  // Pass ``{ limit, offset }`` to walk the rest — see ``withPageParams``.
  listKnowledgeBases: (page?: PageParams) =>
    request<KnowledgeBase[]>("GET", withPageParams("/api/knowledge-bases", page)),
  createKnowledgeBase: (body: { name: string; description?: string; embedding_model_id?: string | null }) =>
    request<KnowledgeBase>("POST", "/api/knowledge-bases", body),
  getKnowledgeBase: (id: string) => request<KnowledgeBase>("GET", `/api/knowledge-bases/${id}`),
  /** PATCH: 省略的字段不动，显式 null 才恢复「继承平台默认」（与服务端一致）。 */
  updateKnowledgeBase: (
    id: string,
    body: {
      name?: string;
      description?: string | null;
      embedding_model_id?: string | null;
    } & Partial<RetrievalSettings>
  ) => request<KnowledgeBase>("PATCH", `/api/knowledge-bases/${id}`, body),
  deleteKnowledgeBase: (id: string) => request("DELETE", `/api/knowledge-bases/${id}`),
  /** What the server will actually accept — the file picker must use this, not a local list. */
  getUploadCapabilities: () =>
    request<UploadCapabilities>("GET", "/api/upload-capabilities"),
  listDocuments: (kbId: string, page?: PageParams) =>
    request<DocFile[]>("GET", withPageParams(`/api/knowledge-bases/${kbId}/documents`, page)),
  uploadDocument: (kbId: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<DocFile>("POST", `/api/knowledge-bases/${kbId}/documents`, fd);
  },
  deleteDocument: (id: string) => request("DELETE", `/api/documents/${id}`),
  reindexDocument: (id: string) => request<ReindexResult>("POST", `/api/documents/${id}/reindex`),
  previewDocument: (id: string, offset = 0) =>
    request<DocumentPreview>(
      "GET",
      `/api/documents/${id}/preview?offset=${offset}`
    ),
  /** Download the original upload (authenticated). */
  downloadDocument: async (id: string): Promise<Blob> => {
    // Route through the central request() so an expired access token is
    // refreshed + the call retried (a raw fetch would just 401 after expiry).
    const res = await request<Response>(
      "GET",
      `/api/documents/${id}/download`,
      undefined,
      { raw: true }
    );
    return res.blob();
  },

  // ---- Retrieval ----
  searchKnowledgeBase: (kbId: string, query: string, topK = 5) =>
    request<{ context: string; citations: Citation[] }>("POST", "/api/retrieval/search", {
      knowledge_base_id: kbId,
      query,
      top_k: topK,
    }),

  // ---- @-references (composer type-ahead) ----
  /** Candidates for the ``@`` picker: the caller's knowledge bases, documents in
   *  them, and this conversation's attachments. Server caps the result set; pass
   *  ``conversationId`` to include the attachment branch (omitting it is a
   *  knowledge-base-only search). */
  searchMentions: (q: string, conversationId?: string | null, limit = 20) => {
    const params = new URLSearchParams({ q, limit: String(limit) });
    if (conversationId) params.set("conversation_id", conversationId);
    return request<MentionList>("GET", `/api/mentions?${params.toString()}`);
  },

  // ---- 语音（ASR 输入 / TTS 播报）----
  /**
   * 能力探测。**不受 SPEECH_ENABLED 拦截** —— 前端必须先能问到「是关的」，
   * 才能把麦克风/喇叭渲染成带中文说明的禁用态，而不是拿到 503 之后靠猜。
   */
  getSpeechCapabilities: () =>
    request<SpeechCapabilities>("GET", "/api/speech/capabilities"),
  /**
   * 录音 → 文本。走中心 request()，所以体积/格式/积分不足都能拿到带 code 的
   * 中文信封（见 `@/lib/voice` 的 code → 文案表）。返回的文本由调用方写进
   * 输入框，**不自动发送**。
   */
  transcribeAudio: (audio: Blob, filename = "voice.webm", signal?: AbortSignal) => {
    const fd = new FormData();
    // 第三个参数决定 multipart 里的 filename：后端在浏览器把 Blob.type 报成
    // 空串时靠扩展名回落判定格式，所以文件名不能省。
    fd.append("file", audio, filename);
    return request<SpeechTranscribeResult>("POST", "/api/speech/transcribe", fd, {
      signal,
    });
  },
  /**
   * 文本 → 音频（后端分块下发，这里汇成一个 Blob 交给 <audio>）。
   * 用 raw + blob() 而不是让 request() 解析 JSON：这是唯一一个「成功响应不是
   * JSON」的语音端点，失败仍是干净的 JSON 信封。
   */
  synthesizeSpeech: async (text: string, signal?: AbortSignal): Promise<Blob> => {
    const res = await request<Response>(
      "POST",
      "/api/speech/synthesize",
      { text },
      { raw: true, signal }
    );
    return res.blob();
  },

  // ---- 提示词库（条目 34）----
  /** 一页模板：预置在前（后端按 sort_order），其余按最近更新。 */
  listPrompts: (opts?: {
    q?: string;
    category?: string;
    scope?: PromptScope;
    limit?: number;
    offset?: number;
  }) => {
    const params = new URLSearchParams();
    if (opts?.q) params.set("q", opts.q);
    if (opts?.category) params.set("category", opts.category);
    if (opts?.scope && opts.scope !== "all") params.set("scope", opts.scope);
    if (opts?.limit != null) params.set("limit", String(opts.limit));
    if (opts?.offset != null) params.set("offset", String(opts.offset));
    const qs = params.toString();
    return request<PromptTemplate[]>(
      "GET",
      `/api/prompts${qs ? `?${qs}` : ""}`
    );
  },
  /** 已有分类名（按用量排序）——筛选标签的数据源，不是前端常量表。 */
  listPromptCategories: (scope?: PromptScope) =>
    request<string[]>(
      "GET",
      `/api/prompts/categories${scope && scope !== "all" ? `?scope=${scope}` : ""}`
    ),
  getPrompt: (id: string) => request<PromptTemplate>("GET", `/api/prompts/${id}`),
  createPrompt: (body: PromptTemplateInput) =>
    request<PromptTemplate>("POST", "/api/prompts", body),
  /** PATCH：省略的字段不动（与 knowledge-bases 同一套语义）。 */
  updatePrompt: (id: string, body: Partial<PromptTemplateInput>) =>
    request<PromptTemplate>("PATCH", `/api/prompts/${id}`, body),
  deletePrompt: (id: string) => request("DELETE", `/api/prompts/${id}`),

  // ---- 消息编辑 / 截断 / 历史版本（条目 30、31、33）----
  /** 改正文；被改掉的原文由服务端先存成一版。**不**顺带截断后续轮次。 */
  updateMessageContent: (messageId: string, content: string) =>
    request<Message>("PATCH", `/api/messages/${messageId}`, { content }),
  /** 删掉这条消息之后的所有消息（每条删除前先存档）。不可逆，界面须显式确认。 */
  truncateMessagesAfter: (messageId: string) =>
    request<MessageTruncateResult>("DELETE", `/api/messages/${messageId}/after`),
  /**
   * 一段会话的历史版本，新的在前。按会话而不是按消息取：重新生成会换掉消息行
   * 的 id，只按 message_id 查恰好漏掉「回答被换掉」那批版本。
   */
  listConversationVersions: (conversationId: string, messageId?: string) =>
    request<MessageVersionDTO[]>(
      "GET",
      `/api/conversations/${conversationId}/versions${
        messageId ? `?message_id=${messageId}` : ""
      }`
    ),
  activateMessageVersion: (
    conversationId: string,
    messageId: string,
    versionId: string
  ) =>
    request<VersionActivateResult>(
      "POST",
      `/api/conversations/${conversationId}/messages/${messageId}/versions/${versionId}/activate`
    ),

  // ---- Tools ----
  listTools: () => request<ToolInfo[]>("GET", "/api/tools"),

  // ---- Admin ----
  adminListUsers: () => request<User[]>("GET", "/api/admin/users"),
  adminUpdateUser: (id: string, body: { role?: string; is_active?: boolean }) =>
    request<User>("PATCH", `/api/admin/users/${id}`, body),
  adminStats: () =>
    request<{ usage: unknown[]; status: unknown }>("GET", "/api/admin/stats"),

  /**
   * 运营开关的**生效结论**（条目 34④）。只读，且回的是算过的结论不是环境变量原文：
   * 引擎要总开关与灰度名单同时成立，`python_exec` 在生产要显式放行 **且** 真有隔离
   * 后端。把 `.env` 抄给运营等于让他们自己重推一遍判定式，而推错的方向永远是
   * 「以为已经开了」。
   */
  adminFeatureFlags: () =>
    request<FeatureFlagPage>("GET", "/api/admin/feature-flags"),

  /**
   * 后台工具目录：**含已被停用的那些**（与 ``listTools`` 的关键差别）。
   *
   * 用户侧那份把停用的工具直接藏掉，这一份不能藏 —— 界面上看不见一个工具，就没有
   * 第二个地方能把它重新打开。
   */
  adminListTools: () => request<ToolInfo[]>("GET", "/api/admin/tools"),

  /**
   * 启用 / 停用一个工具。``note`` 允许留空，但界面上停用时会追问一次：一周后没人
   * 记得当初为什么关，"重新打开"就成了一次无人负责的赌博。
   *
   * 生效是异步的（各进程最长 ``SNAPSHOT_TTL_SECONDS`` = 15s 内跟上），所以返回值只
   * 代表这一行的新状态，不代表"此刻所有 run 都已停手"—— 文案别写成"已生效"。
   */
  adminSetToolEnabled: (name: string, enabled: boolean, note?: string) =>
    request<ToolInfo>("POST", `/api/admin/tools/${encodeURIComponent(name)}/toggle`, {
      enabled,
      note: note?.trim() || null,
    }),

  /**
   * 用量报表（按天 / 按模型 / 按用户）。聚合、分组、分页都在 SQL 里做：
   * `messages` 表会一直长，把行拉到浏览器再累加等于把报表变成一次全表下载。
   *
   * `start` / `end` 是 UTC 日历日且**两端都含当天**；都不传时后端给最近 30 天
   * （`backend/app/services/admin_service.py:34`），响应里的 `start`/`end` 是
   * 实际生效的区间，前端拿它回显与导出，不自己算「今天」。
   */
  adminUsageReport: (query: AdminUsageQuery = {}) => {
    const qs = new URLSearchParams();
    if (query.start) qs.set("start", query.start);
    if (query.end) qs.set("end", query.end);
    if (query.groupBy) qs.set("group_by", query.groupBy);
    if (query.limit != null) qs.set("limit", String(query.limit));
    if (query.offset) qs.set("offset", String(query.offset));
    const q = qs.toString();
    return request<AdminUsagePage>("GET", `/api/admin/usage${q ? `?${q}` : ""}`);
  },

  /**
   * 审计事件列表：action / 操作人 / 目标关键字 / 日期区间都在服务端筛。
   * 一旦分页，前端手上只有这一页，在浏览器里过滤等于「只在这一页里找」。
   */
  adminAuditLog: (query: AdminAuditQuery = {}) => {
    const qs = new URLSearchParams();
    if (query.action) qs.set("action", query.action);
    if (query.actionPrefix) qs.set("action_prefix", query.actionPrefix);
    if (query.actor) qs.set("actor", query.actor);
    if (query.q) qs.set("q", query.q);
    if (query.start) qs.set("start", query.start);
    if (query.end) qs.set("end", query.end);
    if (query.limit != null) qs.set("limit", String(query.limit));
    if (query.offset) qs.set("offset", String(query.offset));
    const q = qs.toString();
    return request<AdminAuditPage>("GET", `/api/admin/audit${q ? `?${q}` : ""}`);
  },

  // ---- Credits（积分） ----
  fetchCredits: () => request<CreditAccountInfo>("GET", "/api/credits/me"),

  redeemCode: (code: string) =>
    request<RedeemResult>("POST", "/api/credits/redeem", { code }),

  fetchCreditLedger: (limit = 50, cursor?: string | null) => {
    const qs = new URLSearchParams({ limit: String(limit) });
    if (cursor) qs.set("cursor", cursor);
    return request<CreditLedgerPage>("GET", `/api/credits/ledger?${qs.toString()}`);
  },

  // ---- Credits（管理端） ----
  /**
   * 批次分页 + 筛选都在服务端（`lib/redeem-batch.ts` 的 `redeemBatchQuery` 是
   * 唯一的条件来源）。后端没有总数端点，所以「装满一页」就是还有下一页的信号。
   */
  adminListRedeemBatches: (query: RedeemBatchQuery) => {
    const params = new URLSearchParams();
    params.set("limit", String(query.limit));
    params.set("offset", String(query.offset));
    if (query.search) params.set("search", query.search);
    if (query.status) params.set("status", query.status);
    return request<RedeemBatchProgress[]>(
      "GET",
      `/api/admin/redeem-batches?${params.toString()}`
    );
  },

  adminCreateRedeemBatch: (body: {
    name: string;
    credits_per_code: number;
    count: number;
    expires_at?: string | null;
    note?: string | null;
  }) => request<RedeemBatchCreateResult>("POST", "/api/admin/redeem-batches", body),

  /**
   * 单码列表（可按批次 / 状态过滤，含核销人）。返回的是「前缀 + 掩码」，
   * 系统里从生成响应之后就不存在明文 —— 后端只存 HMAC 哈希。
   */
  adminQueryRedeemCodes: (opts: {
    batchId?: string | null;
    status?: string | null;
    limit?: number;
    offset?: number;
  } = {}) => {
    const qs = new URLSearchParams();
    if (opts.batchId) qs.set("batch_id", opts.batchId);
    if (opts.status) qs.set("status", opts.status);
    if (opts.limit != null) qs.set("limit", String(opts.limit));
    if (opts.offset != null) qs.set("offset", String(opts.offset));
    const q = qs.toString();
    return request<RedeemCodePage>(
      "GET",
      `/api/admin/redeem-codes${q ? `?${q}` : ""}`
    );
  },

  /** 作废单张未使用的码（已兑换 → 409）。 */
  adminVoidRedeemCode: (codeId: string) =>
    request<RedeemCodeActionResult>("POST", `/api/admin/redeem-codes/${codeId}/void`),

  /** 删除单张未使用的码（已兑换 → 409；误生成时的清理手段）。 */
  adminDeleteRedeemCode: (codeId: string) =>
    request<RedeemCodeActionResult>("DELETE", `/api/admin/redeem-codes/${codeId}`),

  adminVoidRedeemBatch: (batchId: string) =>
    request<{ voided: number }>(
      "POST",
      `/api/admin/redeem-batches/${batchId}/void`
    ),

  adminListCreditAccounts: (search?: string) => {
    const qs = new URLSearchParams();
    if (search) qs.set("search", search);
    return request<CreditAccountRow[]>(
      "GET",
      `/api/admin/credits/accounts${qs.toString() ? `?${qs.toString()}` : ""}`
    );
  },

  adminAdjustCredits: (body: { user_id: string; delta: number; note?: string | null }) =>
    request<CreditAccountRow>("POST", "/api/admin/credits/adjust", body),

  // ---- 管理端：Agent 运行时观测（跨用户，服务端分页） ----
  /**
   * 跨用户的运行列表。用户侧的 ``/api/agent-runs`` 为实时轮询优化（一次带回
   * 全量 steps/graph），管理侧要的是翻页 + 过滤 + 账，所以走这个独立端点。
   * ``status`` 只接受后端白名单里的取值，传错会得到 400。
   */
  adminListAgentRuns: (opts: {
    status?: string | null;
    flowName?: string | null;
    runtime?: string | null;
    q?: string | null;
    conversationId?: string | null;
    limit?: number;
    offset?: number;
  } = {}) => {
    const qs = new URLSearchParams();
    if (opts.status) qs.set("status", opts.status);
    if (opts.flowName) qs.set("flow_name", opts.flowName);
    if (opts.runtime) qs.set("runtime", opts.runtime);
    if (opts.q) qs.set("q", opts.q);
    if (opts.conversationId) qs.set("conversation_id", opts.conversationId);
    if (opts.limit != null) qs.set("limit", String(opts.limit));
    if (opts.offset != null) qs.set("offset", String(opts.offset));
    const q = qs.toString();
    return request<AdminAgentRunPage>("GET", `/api/admin/agent-runs${q ? `?${q}` : ""}`);
  },

  /** 观测面板的汇总卡（默认近 24 小时）。 */
  adminAgentRunsSummary: () =>
    request<AdminRunSummary>("GET", "/api/admin/agent-runs/summary"),

  /** 一个运行收下的持久控制命令（含计划门上闸/撤闸、审批、取消）。 */
  adminListRunCommands: (runId: string, limit = 100) =>
    request<AdminRunCommandRow[]>(
      "GET",
      `/api/admin/agent-runs/${runId}/commands?limit=${limit}`
    ),

  /** ``run_events`` 的分页审计视图（实时跟随仍用 /events SSE）。 */
  adminListRunEvents: (runId: string, limit = 200, offset = 0) =>
    request<AdminRunEventPage>(
      "GET",
      `/api/admin/agent-runs/${runId}/events?limit=${limit}&offset=${offset}`
    ),

  // ---- Agent runs (Phase 3) ----
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
  /**
   * 计划门（B8）：上闸让运行在下一个计划边界停下等人工确认，撤闸放回默认的
   * 「计划先行但不阻塞」。终态运行会被后端拒绝（``ok:false`` + 原状态），
   * 这是唯一的冲突面 —— 后端没有版本/If-Match 乐观锁，命令队列按 created_at
   * 追加，所以调用方拿到 ok:false 时必须重新拉取运行详情再决定重试。
   */
  setPlanGate: (runId: string, enabled: boolean) =>
    request<RunActionResult>("POST", `/api/agent-runs/${runId}/gate`, { enabled }),
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
  /** 改名 / 改描述 / 改颜色：只发送显式给出的字段（omit = 不动）。 */
  update: (id: string, body: ProjectPatch) =>
    request<Project>("PATCH", `/api/projects/${id}`, body),
  /** 删除前向服务端要一次真实的影响范围（会话数 / 消息数）。 */
  impact: (id: string) => request<ProjectImpact>("GET", `/api/projects/${id}/impact`),
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
  /**
   * Server-converted PDF render of an Office artifact (类飞书预览). First
   * call converts via Gotenberg (slow); subsequent calls stream the cached
   * derived PDF. Same auth path as download.
   */
  preview: async (id: string): Promise<Blob> => {
    const res = await request<Response>(
      "GET",
      `/api/artifacts/${id}/preview`,
      undefined,
      { raw: true },
    );
    return res.blob();
  },
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
