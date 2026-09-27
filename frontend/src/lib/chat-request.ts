"use client";

import type { ChatMention, ChatRequest, UserChatMode } from "@/lib/types";

export interface BuildChatBodyOpts {
  conversationId?: string | null;
  modelId?: string | null;
  knowledgeBaseId?: string | null;
  /** Per-turn multi-KB selection (search across several KBs at once). */
  knowledgeBaseIds?: string[];
  /** ``@``-references decoded from the composer text (see lib/inline-refs.ts). */
  mentions?: ChatMention[];
  content: string;
  regenerate?: boolean;
  mode?: UserChatMode;
  attachmentIds?: string[];
  /** Reasoning-effort hint; honored when the model supports it. */
  reasoningEffort?: "low" | "medium" | "high";
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
    mentions: o.mentions ?? [],
    content: o.content,
    regenerate: o.regenerate ?? false,
    mode: o.mode ?? "speed",
    attachment_ids: o.attachmentIds ?? [],
    reasoning_effort: o.reasoningEffort,
  };
}
