// Mirrors backend Pydantic schemas in app/schemas/. Keep field names in sync.

export type Role = "system" | "user" | "assistant" | "tool";

/**
 * User-facing capability modes — the ONLY chat-mode concept the UI exposes.
 * The backend IntentRouter maps each to a runtime/profile/tools. Internal
 * execution_mode/agent_profile are never shown to end users.
 */
export type UserChatMode =
  | "auto"
  | "search"
  | "deep_research"
  | "create"
  | "data_analysis"
  | "debate";

export interface User {
  id: string;
  email: string;
  username: string;
  role: "user" | "admin";
  is_active: boolean;
  created_at: string;
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
  supports_vision: boolean;
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
  supports_vision?: boolean;
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

export interface KnowledgeBase {
  id: string;
  user_id: string;
  name: string;
  description: string | null;
  embedding_model_id: string | null;
  document_count: number;
  chunk_count: number;
  created_at: string;
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
  created_at: string;
  updated_at: string;
}

export interface ToolInfo {
  name: string;
  description: string;
  category: string;
  dangerous: boolean;
  parameters: Array<{
    name: string;
    type: string;
    description: string;
    required: boolean;
    default?: unknown;
    enum?: string[] | null;
  }>;
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
  | { event: "agent_status"; data: { run_id: string; agent_id: string; status: string; task_title?: string; started_at?: string; finished_at?: string; duration_ms?: number; output_summary?: string; error?: string } }
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
  content: string;
  regenerate?: boolean;
  /** User-facing capability mode (Phase 1). The backend derives the route. */
  mode?: UserChatMode;
  /** Attachment ids bound to this user message. */
  attachment_ids?: string[];
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
}

// ---- Context panel ----
export type ContextTab = "execution" | "sources" | "files" | "artifact";
