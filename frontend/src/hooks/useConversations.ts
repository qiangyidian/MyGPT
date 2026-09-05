"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Conversation, ConversationDetail } from "@/lib/types";

export const CONVERSATIONS_QUERY_KEY = ["conversations"] as const;
export const CONVERSATION_DETAIL_QUERY_KEY = (id: string) =>
  ["conversation", id] as const;

interface ListOpts {
  archived?: boolean;
  q?: string;
  limit?: number;
  offset?: number;
}

/**
 * Lists the current user's conversations (summary only, no messages). Supports
 * the archived view and title search; defaults to active conversations,
 * pinned-first. Mutations invalidate the list cache.
 */
export function useConversations(opts: ListOpts = {}) {
  const queryClient = useQueryClient();
  const { archived, q, limit, offset } = opts;

  const list = useQuery<Conversation[]>({
    queryKey: [...CONVERSATIONS_QUERY_KEY, { archived: !!archived, q: q ?? "", limit: limit ?? 0, offset: offset ?? 0 }],
    queryFn: () => api.listConversations({ archived, q, limit, offset }),
    // Keep the previous page visible while the next one loads — without this
    // the query data went undefined between switches and consumers flashed
    // their empty/loading states.
    placeholderData: (prev) => prev,
  });

  const createMutation = useMutation({
    mutationFn: (body?: Parameters<typeof api.createConversation>[0]) =>
      api.createConversation(body ?? {}),
    onSuccess: () => {
      // The list query is keyed by {archived,q,limit,offset}, so writing to the
      // base key was a silent no-op; rely on the invalidate to refetch correctly.
      queryClient.invalidateQueries({ queryKey: CONVERSATIONS_QUERY_KEY });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteConversation(id),
    onSuccess: (_data, deletedId) => {
      queryClient.removeQueries({
        queryKey: CONVERSATION_DETAIL_QUERY_KEY(deletedId),
      });
      queryClient.invalidateQueries({ queryKey: CONVERSATIONS_QUERY_KEY });
    },
  });

  const updateMutation = useMutation({
    mutationFn: (args: { id: string; body: Parameters<typeof api.updateConversation>[1] }) =>
      api.updateConversation(args.id, args.body),
    onSuccess: (conv) => {
      queryClient.setQueryData<Conversation>(
        CONVERSATION_DETAIL_QUERY_KEY(conv.id),
        (old) => (old ? { ...old, ...conv } : old)
      );
      // Pin/archive changes can reorder the list.
      queryClient.invalidateQueries({ queryKey: CONVERSATIONS_QUERY_KEY });
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
    update: updateMutation.mutate,
    updateAsync: updateMutation.mutateAsync,
    isUpdating: updateMutation.isPending,
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
    // Keep the PREVIOUS conversation's messages on screen while the newly
    // selected one loads. Without this, opening an uncached conversation went
    // through a `data === undefined → messages = []` frame and the UI flashed
    // the "welcome" empty state before the real messages appeared.
    placeholderData: (prev) => prev,
  });
}
