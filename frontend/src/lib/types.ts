// Mirrors backend Pydantic schemas in app/schemas/. Keep field names in sync.

export type Role = "system" | "user" | "assistant" | "tool";

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
  created_at: string;
  updated_at: string;
}

export interface ConversationDetail extends Conversation {
  messages: Message[];
}

export interface Citation {
  document_id: string;
  document_name: string;
  chunk_id: string | null;
  chunk_index: number;
  snippet: string;
  score: number;
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
  | { event: "plan_created"; data: { summary: string; steps: AgentPlanStep[] } }
  | { event: "step_started"; data: { step_id: string; title: string; type: string; agent?: string } }
  | { event: "step_completed"; data: { step_id: string; status: string } }
  | { event: "tool_call"; data: { id: string; name: string; arguments: Record<string, unknown>; dangerous?: boolean; approval_id?: string } }
  | { event: "tool_result"; data: { id: string; name: string; ok: boolean; result: unknown; error: string | null } }
  | { event: "approval_required"; data: { run_id: string; approval_id: string; tool_name: string; summary: string; risk_level: string; arguments_preview: Record<string, unknown> } }
  | { event: "token"; data: { delta: string } }
  | { event: "citations"; data: { citations: Citation[] } }
  | { event: "done"; data: { message_id: string; finish_reason: string } }
  | { event: "error"; data: { code: string; message: string } };

export interface ChatRequest {
  conversation_id?: string | null;
  model_id?: string | null;
  knowledge_base_id?: string | null;
  content: string;
  regenerate?: boolean;
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
// The richer model supersedes the old ResearchStep; it carries plan/agent/tool/
// review/approval step types plus the lifecycle status the backend emits.
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
}

