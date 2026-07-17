"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Conversation, ConversationDetail } from "@/lib/types";

export const CONVERSATIONS_QUERY_KEY = ["conversations"] as const;
export const CONVERSATION_DETAIL_QUERY_KEY = (id: string) =>
  ["conversation", id] as const;

/**
 * Lists the current user's conversations (summary only, no messages).
 * Caches under CONVERSATIONS_QUERY_KEY and invalidates it on mutations.
 */
export function useConversations() {
  const queryClient = useQueryClient();

  const list = useQuery<Conversation[]>({
    queryKey: CONVERSATIONS_QUERY_KEY,
    queryFn: () => api.listConversations(),
  });

  const createMutation = useMutation({
    mutationFn: (body?: Parameters<typeof api.createConversation>[0]) =>
      api.createConversation(body ?? {}),
    onSuccess: (conv) => {
      queryClient.setQueryData<Conversation[]>(CONVERSATIONS_QUERY_KEY, (old) =>
        old ? [conv, ...old] : [conv]
      );
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteConversation(id),
    onSuccess: (_data, deletedId) => {
      // Remove from list cache.
      queryClient.setQueryData<Conversation[]>(
        CONVERSATIONS_QUERY_KEY,
        (old) => (old ?? []).filter((c) => c.id !== deletedId)
      );
      // Invalidate detail cache for the deleted conversation.
      queryClient.removeQueries({
        queryKey: CONVERSATION_DETAIL_QUERY_KEY(deletedId),
      });
    },
  });

  return {
    conversations: list.data ?? [],
    isLoading: list.isLoading,
    isError: list.isError,
    error: list.error,
    refetch: list.refetch,
    create: createMutation.mutateAsync,
    createAsync: createMutation.mutateAsync,
    isCreating: createMutation.isPending,
    delete: deleteMutation.mutate,
    deleteAsync: deleteMutation.mutateAsync,
    isDeleting: deleteMutation.isPending,
  };
}

/**
 * Fetches a single conversation with its messages. Use when displaying
 * the active conversation in the message list.
 */
export function useConversationDetail(id: string | null) {
  return useQuery<ConversationDetail | null>({
    queryKey: id ? CONVERSATION_DETAIL_QUERY_KEY(id) : ["conversation", "none"],
    queryFn: () => {
      if (!id) return null;
      return api.getConversation(id);
    },
    enabled: !!id,
  });
}
