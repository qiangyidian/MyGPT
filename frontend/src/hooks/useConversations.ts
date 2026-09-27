"use client";

import { useEffect, useState } from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
} from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Conversation, ConversationDetail } from "@/lib/types";
import {
  CONVERSATION_PAGE_SIZE,
  conversationMatchesQuery,
  conversationPage,
  flattenConversationPages,
  loadedConversationCount,
  nextConversationOffset,
  withConversationPrepended,
  withConversationRemoved,
  withConversationReplaced,
  type ConversationPage,
} from "@/lib/conversation-list";

export const CONVERSATIONS_QUERY_KEY = ["conversations"] as const;
export const CONVERSATION_DETAIL_QUERY_KEY = (id: string) =>
  ["conversation", id] as const;

/** 搜索防抖：列表是服务端搜索，不能每按一个键发一次请求。 */
export const CONVERSATION_SEARCH_DEBOUNCE_MS = 300;

type PagesData = InfiniteData<ConversationPage, number>;

interface ListOpts {
  archived?: boolean;
  /** 已防抖的服务端搜索词（标题 + 末条预览，大小写不敏感）。 */
  q?: string;
  limit?: number;
}

/**
 * Debounce a fast-changing value (a search box) before it becomes a query param.
 * The last committed value survives unmount, so clearing the box commits "".
 */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState<T>(value);

  useEffect(() => {
    if (value === debounced) return;
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, debounced, delayMs]);

  return debounced;
}

/**
 * The sidebar's conversation list: one INFINITE query per (archived, q) view.
 *
 * Why infinite and not a `limit: 50` one-shot: a user with more than one page of
 * history could previously never reach the rest of it.
 *
 * Offset drift (the real bug with server-side offset paging): creating a
 * conversation at the top shifts every later row down, so a naive
 * `offset = lastOffset + pageSize`续页 would either repeat a row or skip one.
 * Two rules make that safe, both in `lib/conversation-list.ts`:
 *  1. the next page's offset is the number of DISTINCT rows the cache holds, so
 *     a shifted window can only ever OVERLAP (repeats) — never gap;
 *  2. repeats are collapsed by id when the pages are flattened, keeping the copy
 *     from the earliest (freshestly refetched) page.
 * On top of that a successful create is written into page 1 rather than
 * invalidating, so the common "new chat while scrolled deep" case never drifts
 * at all. `fetched` (what the server actually returned) — not `items.length` —
 * decides whether another page exists, so local cache writes can't fake the end
 * of the list.
 */
export function useConversations(opts: ListOpts = {}) {
  const queryClient = useQueryClient();
  const archived = !!opts.archived;
  const q = (opts.q ?? "").trim();
  const limit = opts.limit ?? CONVERSATION_PAGE_SIZE;

  // All pages of one view live under THIS key, so every existing
  // `invalidateQueries({ queryKey: CONVERSATIONS_QUERY_KEY })` (chat stream,
  // message actions, projects) keeps covering the whole paginated list.
  const listKey = [...CONVERSATIONS_QUERY_KEY, { archived, q, limit }] as const;

  const list = useInfiniteQuery({
    queryKey: listKey,
    queryFn: async ({ pageParam }) =>
      conversationPage(
        await api.listConversations({
          archived,
          q: q || undefined,
          limit,
          offset: pageParam,
        }),
        pageParam,
        limit
      ),
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) => nextConversationOffset(lastPage, allPages),
    // Keep the previous result set on screen while the next keystroke's page
    // loads — without it the sidebar flashed its empty state between queries.
    // Only within the SAME view, though: carrying the active list over into the
    // archived view (or the other way round) would show rows the user did not
    // ask for, so a view switch really does go through the loading state.
    placeholderData: (prev, prevQuery) => {
      const prevKey = prevQuery?.queryKey[1] as { archived?: boolean } | undefined;
      return prevKey?.archived === archived ? prev : undefined;
    },
  });

  const writePages = (
    edit: (pages: readonly ConversationPage[]) => ConversationPage[]
  ) =>
    queryClient.setQueryData<PagesData>(listKey, (old) =>
      old ? { ...old, pages: edit(old.pages) } : old
    );

  const conversations = flattenConversationPages(list.data?.pages);

  const createMutation = useMutation({
    mutationFn: (body?: Parameters<typeof api.createConversation>[0]) =>
      api.createConversation(body ?? {}),
    onSuccess: (conv) => {
      // 属于当前视图 → 直接写进第一页（不重新拉取，也就不会漂移）。
      if (!archived && conversationMatchesQuery(conv, q)) {
        writePages((pages) => withConversationPrepended(pages, conv));
        return;
      }
      // 搜索词把它挡在视图外 / 当前看的是归档：标记为过期即可，
      // 不做会覆盖刚写入结果的整表重取。
      queryClient.invalidateQueries({
        queryKey: CONVERSATIONS_QUERY_KEY,
        refetchType: "none",
      });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteConversation(id),
    onSuccess: (_data, deletedId) => {
      queryClient.removeQueries({
        queryKey: CONVERSATION_DETAIL_QUERY_KEY(deletedId),
      });
      // Immediate, drift-free local removal …
      writePages((pages) => withConversationRemoved(pages, deletedId));
      // … plus the conventional invalidation: one key covers every loaded page,
      // so a delete still refreshes the whole paginated list (a row deleted
      // server-side shifts the window UP, which is the one direction where an
      // offset page can skip a row — the refetch re-reads it contiguously).
      queryClient.invalidateQueries({ queryKey: CONVERSATIONS_QUERY_KEY });
    },
  });

  const updateMutation = useMutation({
    mutationFn: (args: { id: string; body: Parameters<typeof api.updateConversation>[1] }) =>
      api.updateConversation(args.id, args.body),
    onSuccess: (conv) => {
      queryClient.setQueryData<ConversationDetail>(
        CONVERSATION_DETAIL_QUERY_KEY(conv.id),
        (old) => (old ? { ...old, ...conv } : old)
      );
      const inView = conv.is_archived === archived && conversationMatchesQuery(conv, q);
      writePages((pages) =>
        inView ? withConversationReplaced(pages, conv) : withConversationRemoved(pages, conv.id)
      );
      // Pin/archive/title edits are mirrored straight into the pages, so the
      // list does not need to re-fetch N pages on every rename. With an active
      // search the edit can move a row in or out of the result set, which the
      // local write cannot model — refetch that one view instead.
      queryClient.invalidateQueries({
        queryKey: CONVERSATIONS_QUERY_KEY,
        refetchType: q ? "active" : "none",
      });
    },
  });

  return {
    conversations,
    isLoading: list.isLoading,
    /** 第一页还没来过（用来区分「正在加载」与「真的没有会话」）。 */
    isPending: list.isPending,
    /** 任意一页正在取数（含「加载更多」）。 */
    isFetching: list.isFetching,
    isError: list.isError,
    error: list.error,
    refetch: list.refetch,
    hasNextPage: !!list.hasNextPage,
    hasMore: !!list.hasNextPage,
    isLoadingMore: list.isFetchingNextPage,
    loadedCount: loadedConversationCount(list.data?.pages),
    loadMore: () => {
      if (list.hasNextPage && !list.isFetchingNextPage) void list.fetchNextPage();
    },
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
