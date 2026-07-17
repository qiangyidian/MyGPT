"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ModelConfig } from "@/lib/types";

export const MODELS_QUERY_KEY = ["models"] as const;

/**
 * Fetches all model configs; chat-capable models are those that are NOT
 * embedding-only. The hook returns both the raw list and a convenience
 * `chatModels` filtered array.
 */
export function useModels() {
  const query = useQuery<ModelConfig[]>({
    queryKey: MODELS_QUERY_KEY,
    queryFn: () => api.listModels(),
  });

  const chatModels = (query.data ?? []).filter((m) => !m.is_embedding);

  return { ...query, chatModels };
}
