// Mirrors backend Pydantic schemas in app/schemas/. Keep field names in sync.

export type Role = "system" | "user" | "assistant" | "tool";

/**
 * User-facing capability modes — the ONLY chat-mode concept the UI exposes.
 * The backend IntentRouter maps each to a runtime/profile/tools. Internal
 * execution_mode/agent_profile are never shown to end users.
 */
export type UserChatMode =
  | "speed"
  | "expert"
  | "debate"
  | "hermes"
  // Legacy values kept for displaying older conversations / backward compat:
  | "auto"
  | "search"
  | "deep_research"
  | "create"
  | "data_analysis";

export interface User {
  id: string;
  email: string;
  username: string;
  role: "user" | "admin";
  is_active: boolean;
  created_at: string;
}

/** Public login-page hints for Official Account scan login. */
export interface WechatLoginInfo {
  /** False when the deployment has no QR image set — show a text hint instead. */
  configured: boolean;
  qrcode_url: string;
  /** Keyword an already-following user sends to the account to get a code. */
  keyword: string;
}

export interface WechatBinding {
  bound: boolean;
  openid: string | null;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: User;
}

export interface ModelConfig {
  id: string;
  user_id: string | null;
  name: string;
  provider: string;
  api_base_url: string;
  api_key_masked: string;
  has_key: boolean;
  model_name: string;
  embedding_model_name: string | null;
  supports_stream: boolean;
  supports_tools: boolean;
  supports_parallel_tools: boolean;
  supports_vision: boolean;
  supports_audio_input: boolean;
  supports_audio_output: boolean;
  supports_image_generation: boolean;
  supports_structured_output: boolean;
  supports_reasoning_effort: boolean;
  output_token_parameter: "max_tokens" | "max_completion_tokens";
  max_context_tokens: number;
  max_tokens: number;
  temperature: number;
  top_p: number;
  is_embedding: boolean;
  created_at: string;
}

export interface ModelConfigInput {
  name: string;
  provider?: string;
  api_base_url: string;
  api_key?: string | null;
  model_name: string;
  embedding_model_name?: string | null;
  supports_stream?: boolean;
  supports_tools?: boolean;
  supports_parallel_tools?: boolean;
  supports_vision?: boolean;
  supports_audio_input?: boolean;
  supports_audio_output?: boolean;
  supports_image_generation?: boolean;
  supports_structured_output?: boolean;
  supports_reasoning_effort?: boolean;
  output_token_parameter?: "max_tokens" | "max_completion_tokens";
  max_context_tokens?: number;
  max_tokens?: number;
  temperature?: number;
  top_p?: number;
  is_embedding?: boolean;
}

export interface ModelTestResult {
  ok: boolean;
  latency_ms: number;
  sample: string | null;
  error: string | null;
}

/**
 * Canonical termination reason, carried end-to-end (provider → runtime → SSE →
 * persisted metadata → UI). Mirrors the backend Literal.
 */
export type FinishReason =
  | "stop"
  | "length"
  | "tool_calls"
  | "cancelled"
  | "timeout"
  | "content_filter"
  | "provider_error"
  | "stream_disconnected"
  | "budget"
  | "error"
  | "aborted"; // legacy FE-only value (mapped to cancelled on display)

/** Consumer-facing generation status, derived from finish_reason. */
export type GenerationStatus =
  | "complete"
  | "truncated"
  | "cancelled"
  | "error"
  | "interrupted";

const FINISH_TO_STATUS: Record<FinishReason, GenerationStatus> = {
  stop: "complete",
  tool_calls: "complete",
  length: "truncated",
  budget: "truncated",
  cancelled: "cancelled",
  aborted: "cancelled",
  timeout: "error",
  content_filter: "error",
  provider_error: "error",
  error: "error",
  stream_disconnected: "interrupted",
};

/** Read the persisted finish_reason off a message's metadata (typed). */
export function getMessageFinishReason(msg: Message): FinishReason | null {
  const v = msg.metadata?.finish_reason;
  return typeof v === "string" ? (v as FinishReason) : null;
}

/** Derive the consumer-facing status from a message's finish_reason. */
export function getMessageStatus(msg: Message): GenerationStatus | null {
  const fr = getMessageFinishReason(msg);
  if (!fr) return null;
  return FINISH_TO_STATUS[fr] ?? "error";
}

/** Derive the consumer-facing status directly from a finish_reason. */
export function finishReasonToStatus(fr: FinishReason): GenerationStatus {
  return FINISH_TO_STATUS[fr] ?? "error";
}

/** True when a message ended abnormally (and thus may warrant "continue"). */
export function isPartialResult(msg: Message): boolean {
  const st = getMessageStatus(msg);
  return st === "truncated" || st === "interrupted" || st === "cancelled";
}

export interface Message {
  id: string;
  conversation_id: string;
  role: Role;
  content: string;
  metadata: Record<string, unknown>;
  model_name: string | null;
  created_at: string;
}

export interface Conversation {
  id: string;
  user_id: string;
  title: string;
  model_id: string | null;
  knowledge_base_id: string | null;
  system_prompt: string | null;
  is_pinned: boolean;
  is_archived: boolean;
  last_message_preview: string | null;
  parent_conversation_id: string | null;
  branch_from_message_id: string | null;
  /** Soft reference to a Project (Phase 3); null = unfiled. */
  project_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface Project {
  id: string;
  name: string;
  description: string | null;
  color: string;
  created_at: string;
  updated_at: string;
}

export interface ProjectInput {
  name: string;
  description?: string | null;
  color?: string | null;
}

/**
 * PATCH body for a project. Omitted keys are left untouched server-side;
 * `color: null` restores the platform default while `name: null` is a 400.
 */
export interface ProjectPatch {
  name?: string | null;
  description?: string | null;
  color?: string | null;
}

/**
 * What deleting a project actually costs, counted by the server
 * (`GET /api/projects/{id}/impact`) instead of guessed in the dialog.
 * `deletes_conversations` is false today: `Conversation.project_id` is a soft
 * reference with no FK, so the delete un-files conversations rather than
 * removing them.
 */
export interface ProjectImpact {
  project_id: string;
  name: string;
  conversation_count: number;
  archived_conversation_count: number;
  message_count: number;
  knowledge_base_count: number;
  deletes_conversations: boolean;
}

export interface ConversationDetail extends Conversation {
  messages: Message[];
}

/** A per-message/per-conversation file attachment summary stored on the message. */
export interface AttachmentRef {
  id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  status: string;
  parse_status?: string;
}

/** A full chat attachment row (from /api/chat-attachments). */
/** Parsed-text preview served by GET /api/chat-attachments/{id}/text. */
export interface AttachmentTextPreview {
  id: string;
  filename: string;
  mime_type: string;
  parse_status: string;
  preview_metadata: Record<string, unknown> | null;
  text: string;
  truncated: boolean;
  total_chars: number;
}

export interface ChatAttachment {
  id: string;
  conversation_id: string;
  message_id: string | null;
  filename: string;
  original_filename: string;
  mime_type: string;
  size_bytes: number;
  status: "uploading" | "uploaded" | "parsing" | "ready" | "failed" | "deleted";
  parse_status: "pending" | "parsing" | "ready" | "failed" | "skipped";
  preview_metadata: Record<string, unknown> | null;
  error_message: string | null;
  is_temporary: boolean;
  knowledge_base_id: string | null;
  created_at: string;
  updated_at: string;
}

export type MessageFeedbackRating = "up" | "down";

export interface MessageFeedback {
  id: string;
  message_id: string;
  conversation_id: string;
  rating: MessageFeedbackRating;
  reason: string | null;
  comment: string | null;
  created_at: string;
  updated_at: string;
}

export interface Citation {
  document_id: string | null;
  document_name: string;
  chunk_id: string | null;
  chunk_index: number;
  snippet: string;
  score: number;
  /** web | document | attachment | database */
  source_type?: string;
  url?: string | null;
  attachment_id?: string | null;
  page_number?: number | null;
  published_at?: string | null;
  accessed_at?: string | null;
  /** Reranker score (debug/eval only; not shown to regular users). */
  rerank_score?: number | null;
  metadata?: Record<string, unknown>;
}

/** Per-KB retrieval / chunking overrides. ``null`` means "inherit the platform
 *  default", not "off" — so clearing a field must send an explicit null. */
export interface RetrievalSettings {
  top_k: number | null;
  score_threshold: number | null;
  /** "Allow reranking": on if any selected KB enables it in a cross-KB query. */
  rerank_enabled: boolean | null;
  /** Only affects documents indexed after the change. */
  chunk_size: number | null;
  chunk_overlap: number | null;
}

export interface KnowledgeBase extends RetrievalSettings {
  id: string;
  user_id: string;
  name: string;
  description: string | null;
  embedding_model_id: string | null;
  document_count: number;
  chunk_count: number;
  created_at: string;
}

export interface UploadCapabilities {
  /** Effective server-side allow-list, e.g. [".pdf", ".docx"]. */
  allowed_extensions: string[];
  max_upload_mb: number;
}

export interface DocFile {
  id: string;
  knowledge_base_id: string;
  filename: string;
  file_type: string;
  file_size: number;
  status:
    | "pending"
    | "parsing"
    | "chunking"
    | "embedding"
    | "indexed"
    | "failed";
  error_message: string | null;
  chunk_count: number;
  /** Attempts spent in the ingestion queue (``INGEST_MAX_ATTEMPTS`` bounds it). */
  ingest_attempts: number;
  /** Set while a failed ingestion waits to be retried; null = not scheduled. */
  ingest_next_retry_at: string | null;
  created_at: string;
  updated_at: string;
}

/** POST /api/documents/{id}/reindex — the queue's snapshot after re-queueing. */
export interface ReindexResult {
  document_id: string;
  status: string;
  chunk_count: number;
  ingest_attempts: number;
  ingest_next_retry_at: string | null;
}

/** GET /api/documents/{id}/preview — one page of the parsed full text. */
export interface DocumentPreview {
  document_id: string;
  filename: string;
  file_type: string;
  file_size: number;
  status: string;
  render_as: "markdown" | "text";
  chars: number;
  total_chars: number;
  truncated: boolean;
  content: string;
}

export interface ToolInfo {
  name: string;
  description: string;
  category: string;
  dangerous: boolean;
  /**
   * 运营启停（条目 34③）。可选是因为 ``GET /api/tools``（用户侧目录）根本不含被停用
   * 的工具，那份响应里这个字段没有意义；后台目录才拿它渲染开关。缺省按"可用"处理。
   */
  enabled?: boolean;
  /** 上次停用/启用时留下的理由，界面上要显示出来：没有理由的开关没人敢动。 */
  toggle_note?: string | null;
  parameters: Array<{
    name: string;
    type: string;
    description: string;
    required: boolean;
    default?: unknown;
    enum?: string[] | null;
  }>;
}

// ---- 运营开关的生效结论（条目 34④）----
// 注意 `enabled` 是**算出来的**结论，不是环境变量的原文：引擎要总开关与灰度名单同时
// 成立，python_exec 在生产要显式放行且真有隔离后端。
export interface FeatureFlag {
  key: string;
  label: string;
  group: string;
  enabled: boolean;
  value: string;
  source: string;
  note: string;
}

export interface FeatureFlagPage {
  generated_at: string;
  env: string;
  flags: FeatureFlag[];
}

// ---- SSE stream events from /api/chat/stream ----
export type ChatStreamEvent =
  | { event: "meta"; data: { message_id: string; conversation_id: string } }
  | { event: "run_started"; data: { run_id: string; runtime: string; conversation_id: string; message_id: string } }
  | {
      event: "runtime_selected";
      data: {
        run_id: string;
        requested_mode: string;
        effective_mode: string;
        requested_runtime: string;
        effective_runtime: string;
        agent_profile: string;
        multi_agent_requested: boolean;
        multi_agent_executed: boolean;
        fallback_reason: string | null;
      };
    }
  | { event: "plan_created"; data: { summary: string; steps: AgentPlanStep[] } }
  | { event: "step_started"; data: { step_id: string; title: string; type: string; agent?: string } }
  | { event: "step_completed"; data: { step_id: string; status: string } }
  | { event: "agent_graph"; data: { run_id: string; graph: unknown } }
  | { event: "agent_status"; data: { run_id: string; agent_id: string; status: string; task_title?: string; started_at?: string; finished_at?: string; duration_ms?: number; output_summary?: string; error?: string; usage?: Record<string, number>; cost_usd?: number } }
  | { event: "step_output"; data: { run_id: string; agent_id: string; text: string; truncated: boolean; chars: number } }
  | { event: "step_progress"; data: { run_id: string; agent_id: string; elapsed_s: number; note?: string } }
  | { event: "agent_edge"; data: { run_id: string; edge_id: string; status: string; label?: string } }
  | { event: "run_status"; data: { run_id: string; status: string; current_agent_ids?: string[] } }
  | { event: "tool_call"; data: { id: string; name: string; arguments: Record<string, unknown>; dangerous?: boolean; approval_id?: string; agent_id?: string; task_id?: string } }
  | { event: "tool_result"; data: { id: string; name: string; ok: boolean; result: unknown; error: string | null; agent_id?: string; task_id?: string } }
  | { event: "approval_required"; data: { run_id: string; approval_id: string; tool_name: string; summary: string; risk_level: string; arguments_preview: Record<string, unknown> } }
  | { event: "token"; data: { delta: string } }
  | { event: "citations"; data: { citations: Citation[] } }
  | { event: "research_plan"; data: { run_id: string; status: string; summary: string; steps: ResearchPlanStep[]; requires_confirmation: boolean } }
  | { event: "research_plan_updated"; data: { run_id: string; status: string; summary: string; steps: ResearchPlanStep[]; requires_confirmation: boolean } }
  | { event: "run_instruction_received"; data: { run_id: string; instruction: string; acknowledged: boolean } }
  | { event: "run_paused"; data: { run_id: string; reason: string; paused_at?: string } }
  | { event: "run_resumed"; data: { run_id: string; resumed_at?: string } }
  | { event: "done"; data: { message_id: string; finish_reason: FinishReason } }
  | { event: "error"; data: { code: string; message: string } };

/** One ``@`` reference as it goes on the wire: the stable pair the composer
 *  decoded out of the message text (never a label — a rename cannot change what
 *  a turn retrieves). Mirrors backend ``app/schemas/chat.py::ChatMention``. */
export interface ChatMention {
  kind: "kb" | "doc" | "file";
  id: string;
}

/** One type-ahead candidate from ``GET /api/mentions``. */
export interface MentionTarget {
  kind: ChatMention["kind"];
  id: string;
  /** ``kind:id`` — what ``inline-refs.encodeRef`` puts in the token. */
  token: string;
  label: string;
  sublabel?: string;
  knowledge_base_id?: string | null;
  /** False for a target that exists but cannot be retrieved yet (indexing). */
  selectable?: boolean;
}

/** ``GET /api/mentions`` response: a capped page + the caps the server enforces. */
export interface MentionList {
  items: MentionTarget[];
  truncated: boolean;
  max_knowledge_bases: number;
  max_mentions: number;
}

export interface ResearchPlanStep {
  id: string;
  title: string;
  description?: string;
  sources?: string[];
}

export interface ChatRequest {
  conversation_id?: string | null;
  model_id?: string | null;
  knowledge_base_id?: string | null;
  /** Per-turn multi-KB selection (Phase 1+). */
  knowledge_base_ids?: string[];
  /** ``@``-references typed in the composer (see lib/inline-refs.ts). They
   *  EXTEND the toolbar selection for this turn; the server folds them into the
   *  retrieval scope and rejects any the caller may not read. */
  mentions?: ChatMention[];
  content: string;
  regenerate?: boolean;
  /** User-facing capability mode (Phase 1). The backend derives the route. */
  mode?: UserChatMode;
  /** Attachment ids bound to this user message. */
  attachment_ids?: string[];
  /** Reasoning-effort hint (low|medium|high); honored when the selected model
   *  declares supports_reasoning_effort. */
  reasoning_effort?: "low" | "medium" | "high";
  // ---- legacy fields (still accepted by the backend; not exposed in the UI) ----
  enable_tools?: boolean;
  execution_mode?: "auto" | "chat" | "agent";
  agent_profile?: string;
}

// A short step in a published plan (plan_created event).
export interface AgentPlanStep {
  id: string;
  title: string;
}

// A single agent execution step shown in the "执行过程" panel.
export interface AgentStep {
  id: string;
  sequence: number;
  type: "plan" | "agent" | "tool" | "review" | "approval";
  title: string;
  summary?: string;
  status: "pending" | "running" | "waiting" | "done" | "error";
  startedAt?: string;
  finishedAt?: string;
  tool?: {
    name: string;
    dangerous?: boolean;
    argumentsPreview?: Record<string, unknown>;
    resultPreview?: string;
    ok?: boolean;
  };
}

// Legacy alias kept for back-compat with existing code paths.
export type ResearchStep = AgentStep;

// An in-flight human-approval request for a dangerous tool call.
export interface PendingApproval {
  runId: string;
  approvalId: string;
  toolName: string;
  summary: string;
  riskLevel: string;
  argumentsPreview: Record<string, unknown>;
}

// Agent run detail (GET /api/agent-runs/{id}).
export interface AgentRunStep {
  id: string;
  sequence: number;
  step_type: string;
  agent_name: string;
  agent_id?: string;
  task_id?: string;
  tool_name: string;
  status: string;
  input_redacted: Record<string, unknown> | null;
  output_redacted: Record<string, unknown> | null;
  latency_ms: number | null;
  created_at: string;
}

export interface AgentRunApproval {
  id: string;
  run_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  risk_level: string;
  status: string;
  reason: string | null;
  created_at: string;
  expires_at: string | null;
}

/** A persisted tool call's full input/output — the on-prem audit surface. */
export interface ToolCallAudit {
  id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  result: Record<string, unknown> | null;
  status: string;
  error_message: string | null;
  created_at: string;
}

export interface AgentRun {
  id: string;
  conversation_id: string;
  message_id: string | null;
  runtime: string;
  flow_name: string;
  status: string;
  current_step: string;
  input: Record<string, unknown>;
  output: Record<string, unknown> | null;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  created_at: string;
  steps: AgentRunStep[];
  approvals: AgentRunApproval[];
  /** Persisted tool-call audit trail (full arguments/result) for this run. */
  tool_calls: ToolCallAudit[];
  /** Multi-agent graph snapshot (null for single-agent / native runs). */
  graph: Record<string, unknown> | null;
  /** Research/agent plan + its lifecycle status (mirrors AgentRunOut). */
  plan: Record<string, unknown> | null;
  plan_status: string | null;
  user_instructions: Record<string, unknown> | null;
  paused_at: string | null;
  /** 计划门是否上着闸（最后一条持久 gate 命令 + plan_status 收口后的结果）。 */
  gate_armed: boolean;
}

// ---- Context panel ----
export type ContextTab = "execution" | "sources" | "files";

// ===========================================================================
// Task 12: durable runs, artifacts, user memories, connectors, typed parts.
// Field names mirror the backend Pydantic schemas (app/schemas/*) so the API
// client can pass them through unchanged.
// ===========================================================================

/**
 * A row in the durable, append-only run-event log
 * (`GET /api/agent-runs/{run_id}/events`, cursor-replay SSE). The SSE frame's
 * `id:` line carries the `sequence`; `event_type` is the durable event name
 * (e.g. `run.started`, `step.completed`) or a chat-stream event name
 * (`run_started`, `token`, `done`).
 */
export interface DurableRunEvent {
  id: string;
  run_id: string;
  sequence: number;
  event_type: string;
  data: Record<string, unknown>;
  created_at: string;
}

/** Workflow status of a durable run (independent of any SSE subscription). */
export type DurableRunStatus =
  | "pending"
  | "running"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

/**
 * Connection status of a run-event subscription. Deliberately SEPARATE from
 * `DurableRunStatus`: a client disconnect (network drop, navigation) flips
 * this to `"disconnected"` but leaves `runStatus` untouched — the workflow
 * keeps running server-side and a reconnect resumes from the cursor.
 */
export type RunSubscriptionStatus =
  | "idle"
  | "connecting"
  | "open"
  | "reconnecting"
  | "disconnected";

/** An artifact row (tenant-scoped; bytes fetched via `/api/artifacts/{id}`). */
export interface ArtifactMeta {
  id: string;
  media_type: string;
  size: number;
  checksum?: string | null;
  filename: string | null;
  source: string;
  created_at: string | null;
}

/** A user-level (cross-conversation, opt-in) semantic memory row. */
export interface UserMemory {
  id: string;
  user_id: string;
  memory_type: string;
  content: string;
  structured_value: Record<string, unknown> | null;
  confidence: number;
  active: boolean;
  confirmed_by_user: boolean;
  source_message_id: string | null;
  source_conversation_id: string | null;
  expires_at: string | null;
  embedding_id: string | null;
  created_at: string;
  updated_at: string;
}

/** Body for `POST /api/memories` (propose a candidate). Defaults to INACTIVE. */
export interface UserMemoryProposeInput {
  content: string;
  memory_type?: string;
  confidence?: number;
  source_message_id?: string | null;
  source_conversation_id?: string | null;
  /** Always false on the wire — the user must opt in by activating. */
  active?: false;
}

/** Body for `PATCH /api/memories/{id}` (edit content; re-embeds if active). */
export interface UserMemoryEditInput {
  content: string;
}

/** A tenant-scoped connector row (credentials NEVER included on reads). */
export interface Connector {
  id: string;
  user_id: string;
  name: string;
  provider: string;
  manifest: Record<string, unknown>;
  transport: string;
  command_or_url: string;
  oauth_scopes: string[];
  enabled: boolean;
  extra: Record<string, unknown> | null;
  last_used_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConnectorCreateInput {
  name: string;
  provider: string;
  credentials: Record<string, unknown>;
  oauth_scopes?: string[];
  command_or_url?: string | null;
  transport?: string | null;
  enabled?: boolean;
  extra?: Record<string, unknown> | null;
}

export interface ConnectorUpdateInput {
  name?: string;
  oauth_scopes?: string[];
  extra?: Record<string, unknown> | null;
}

export interface ProviderManifest {
  name: string;
  kind: string;
  transport: string;
  command_or_url: string;
  required_scopes: string[];
  description: string;
}

/**
 * A typed message part. The backend persists multimodal content as typed parts;
 * the composer emits them and the message bubble renders them. `text` is the
 * default (existing string `content` maps to a single text part).
 */
export type MessagePart =
  | { type: "text"; text: string }
  | { type: "image"; mime_type: string; data: string; filename?: string }
  | { type: "audio"; mime_type: string; data: string; filename?: string }
  | { type: "file"; mime_type: string; data: string; filename?: string }
  | { type: "artifact"; artifact_id: string };

/** Result shape for the durable run-control endpoints (approve/reject/etc). */
export interface RunActionResult {
  ok: boolean;
  status: string;
  message: string | null;
}

// ===========================================================================
// Credits / redeem codes
// ===========================================================================

export interface CreditAccountInfo {
  balance: number;
  lifetime_granted: number;
  lifetime_consumed: number;
  /** 观察模式开关：false 表示余额不足不会拦截。 */
  enforced: boolean;
}

export interface CreditLedgerEntry {
  id: string;
  delta: number;
  balance_after: number;
  reason: "redeem" | "admin_adjust" | "usage" | "signup_bonus" | string;
  ref_type: string | null;
  note: string | null;
  created_at: string;
}

export interface CreditLedgerPage {
  entries: CreditLedgerEntry[];
  next_cursor: string | null;
}

export interface RedeemResult {
  credits_added: number;
  balance: number;
  batch_name: string;
}

export interface RedeemBatchInfo {
  id: string;
  name: string;
  credits_per_code: number;
  expires_at: string | null;
  note: string | null;
  created_at: string;
}

export interface RedeemBatchProgress {
  batch: RedeemBatchInfo;
  total: number;
  redeemed: number;
  void: number;
  active: number;
}

export interface RedeemBatchCreateResult {
  batch: RedeemBatchInfo;
  /** 明文码，仅创建响应返回一次。 */
  codes: string[];
}

export interface RedeemCodeInfo {
  id: string;
  code_prefix: string;
  status: "active" | "redeemed" | "void" | string;
  redeemed_by: string | null;
  redeemed_at: string | null;
  created_at: string;
}

export interface CreditAccountRow {
  user_id: string;
  email: string;
  username: string;
  balance: number;
  lifetime_granted: number;
  lifetime_consumed: number;
}

// ===========================================================================
// 管理端：兑换码单码运营面（GET /api/admin/redeem-codes 等）
// ===========================================================================

/**
 * 一行兑换码。后端只存哈希，所以这里**永远没有明文** —— ``masked_code`` 是
 * 「6 位前缀 + 固定掩码」，够运营辨认，不够还原凭证。
 */
export interface RedeemCodeRow {
  id: string;
  batch_id: string;
  batch_name: string;
  credits_per_code: number;
  code_prefix: string;
  masked_code: string;
  status: "active" | "redeemed" | "void" | string;
  expires_at: string | null;
  redeemed_by: string | null;
  redeemer_email: string | null;
  redeemer_username: string | null;
  redeemed_at: string | null;
  created_at: string;
}

export interface RedeemCodePage {
  items: RedeemCodeRow[];
  total: number;
  limit: number;
  offset: number;
}

/** 单码作废 / 删除的结果：``changed=false`` 表示状态本来就是那样。 */
export interface RedeemCodeActionResult {
  id: string;
  status: string;
  changed: boolean;
  message: string;
}

// ===========================================================================
// 管理端：Agent 运行时观测（GET /api/admin/agent-runs …）
// ===========================================================================

/** 运行列表的一行（跨用户，含账与门状态）。 */
export interface AdminAgentRunRow {
  id: string;
  conversation_id: string;
  conversation_title: string | null;
  user_id: string | null;
  user_email: string | null;
  user_username: string | null;
  runtime: string;
  flow_name: string;
  status: string;
  current_step: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  error_message: string | null;
  plan_status: string;
  plan_present: boolean;
  paused_at: string | null;
  gate_armed: boolean;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number | null;
  credits_consumed: number;
  step_count: number;
  pending_approvals: number;
}

export interface AdminAgentRunPage {
  items: AdminAgentRunRow[];
  total: number;
  limit: number;
  offset: number;
}

/** 观测面板顶部的汇总卡（默认近 24 小时窗口）。 */
export interface AdminRunSummary {
  total_runs: number;
  running: number;
  waiting_approval: number;
  failed: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  avg_duration_ms: number | null;
}

/** 一条持久控制命令（pause/resume/cancel/gate/approve/instruction）。 */
export interface AdminRunCommandRow {
  id: string;
  command_type: string;
  payload: Record<string, unknown>;
  status: string;
  created_at: string;
  applied_at: string | null;
  error: string | null;
}

/** ``run_events`` 表的一行（分页审计视图；实时跟随仍走 SSE）。 */
export interface AdminRunEventRow {
  id: string;
  sequence: number;
  event_type: string;
  data: Record<string, unknown>;
  created_at: string;
}

export interface AdminRunEventPage {
  items: AdminRunEventRow[];
  total: number;
  limit: number;
  offset: number;
}

/**
 * 语音能力探测（`GET /api/speech/capabilities`）—— 服务端把「实际会接受什么」
 * 一次讲清楚：按钮是否可点、体积/时长/字数上限、可录容器。前端**不抄常量**，
 * 全部以此为准（与 `UploadCapabilities` 同定位）。后端事实来源：
 * `app/services/speech_service.capabilities`。
 */
export interface SpeechCapabilities {
  /** 总开关（SPEECH_ENABLED）。关闭时两个端点都会返回中文 503。 */
  enabled: boolean;
  asr_enabled: boolean;
  tts_enabled: boolean;
  /** 不可用的原因："disabled" | "model_unconfigured" | null（一切就绪）。 */
  reason: string | null;
  max_audio_mb: number;
  max_audio_bytes: number;
  /** 录音硬上限（秒）；到点前端自动停止。 */
  max_duration_seconds: number;
  /** 单次播报的字符上限（中文一个字算一个）。 */
  max_text_chars: number;
  /** 服务端白名单，例如 ["audio/flac", "audio/mpeg", ...]。 */
  audio_mime_types: string[];
  tts_response_format: string;
  /** 合成响应 MIME（浏览器可直接播），如 "audio/mpeg"。 */
  tts_mime_type: string;
  tts_voice: string;
  /** 关闭时的官方中文说明，直接展示给用户。 */
  disabled_message: string;
}

/** `POST /api/speech/transcribe` 的响应：识别文本 + 本次请求的元信息。 */
export interface SpeechTranscribeResult {
  text: string;
  media_type: string;
  audio_bytes: number;
  model_name: string;
}

/** 提示词库模板（条目 34）。`user_id` 为 null 表示平台预置。 */
export interface PromptTemplate {
  id: string;
  user_id: string | null;
  title: string;
  content: string;
  category: string;
  tags: string[];
  description: string | null;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export interface PromptTemplateInput {
  title: string;
  content: string;
  category?: string;
  tags?: string[];
  description?: string | null;
}

/** 提示词库列表的可见范围（与后端 `scope` 查询参数同义）。 */
export type PromptScope = "all" | "mine" | "preset";

/** 消息的历史版本（条目 31），与后端 MessageVersionOut 同形。 */
export interface MessageVersionDTO {
  id: string;
  message_id: string;
  conversation_id: string;
  role: string;
  content: string;
  metadata: Record<string, unknown>;
  model_name: string | null;
  total_tokens: number | null;
  cost_usd: number | null;
  origin: string;
  created_at: string;
}

export interface VersionActivateResult {
  message_id: string;
  activated_version_id: string;
  changed: boolean;
}

export interface MessageTruncateResult {
  conversation_id: string;
  deleted: number;
  last_message_preview: string | null;
}
