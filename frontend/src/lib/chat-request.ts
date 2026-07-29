"use client";

import type { ChatRequest, UserChatMode } from "@/lib/types";

export interface BuildChatBodyOpts {
  conversationId?: string | null;
  modelId?: string | null;
  knowledgeBaseId?: string | null;
  /** Per-turn multi-KB selection (search across several KBs at once). */
  knowledgeBaseIds?: string[];
  content: string;
  regenerate?: boolean;
  mode?: UserChatMode;
  attachmentIds?: string[];
}

/**
 * Build the POST /api/chat/stream body from user-facing options. The UI never
 * references internal runtime enums (execution_mode/agent_profile) — it only
 * sends the stable ``mode`` + attachment ids, and the backend IntentRouter
 * decides the runtime/profile/tools.
 */
export function buildChatBody(o: BuildChatBodyOpts): ChatRequest {
  return {
    conversation_id: o.conversationId ?? null,
    model_id: o.modelId ?? null,
    knowledge_base_id: o.knowledgeBaseId ?? null,
    knowledge_base_ids: o.knowledgeBaseIds ?? [],
    content: o.content,
    regenerate: o.regenerate ?? false,
    mode: o.mode ?? "auto",
    attachment_ids: o.attachmentIds ?? [],
  };
}
