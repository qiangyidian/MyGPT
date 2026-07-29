"use client";

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { api } from "@/lib/api";
import type { ModelConfig } from "@/lib/types";

export const MODELS_QUERY_KEY = ["models"] as const;

/**
 * Fetches all model configs; chat-capable models are those that are NOT
 * embedding-only. The hook returns both the raw list and a convenience
 * `chatModels` filtered array (memoized for a stable reference).
 */
export function useModels() {
  const query = useQuery<ModelConfig[]>({
    queryKey: MODELS_QUERY_KEY,
    queryFn: () => api.listModels(),
  });

  // Memoize: a fresh array each render caused unnecessary effect re-runs in
  // consumers (the same Object.is instability class as the EMPTY_DRAFTS bug).
  const chatModels = useMemo(
    () => (query.data ?? []).filter((m) => !m.is_embedding),
    [query.data]
  );

  return { ...query, chatModels };
}
