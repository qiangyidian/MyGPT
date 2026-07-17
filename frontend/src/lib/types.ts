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
  | { event: "token"; data: { delta: string } }
  | { event: "citations"; data: { citations: Citation[] } }
  | { event: "tool_call"; data: { id: string; name: string; arguments: Record<string, unknown> } }
  | { event: "tool_result"; data: { id: string; name: string; ok: boolean; result: unknown; error: string | null } }
  | { event: "done"; data: { message_id: string; finish_reason: string } }
  | { event: "error"; data: { code: string; message: string } };

export interface ChatRequest {
  conversation_id?: string | null;
  model_id?: string | null;
  knowledge_base_id?: string | null;
  content: string;
  regenerate?: boolean;
  enable_tools?: boolean;
}

// A single agent step (tool call + its result) shown in the "research" panel.
export interface ResearchStep {
  id: string;
  name: string;
  arguments?: Record<string, unknown>;
  result?: string;
  status: "running" | "done" | "error";
}

