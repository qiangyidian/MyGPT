"use client";

import { useQuery } from "@tanstack/react-query";

import { memoriesApi } from "@/lib/api";
import { USER_MEMORIES_QUERY_KEY } from "@/lib/memories";

/** Shared cache for the chat affordance and the full memory manager. */
export function useUserMemories(enabled = true) {
  return useQuery({
    queryKey: USER_MEMORIES_QUERY_KEY,
    queryFn: memoriesApi.list,
    enabled,
    staleTime: 30_000,
  });
}
