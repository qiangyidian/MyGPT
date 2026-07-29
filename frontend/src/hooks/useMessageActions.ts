"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import {
  CONVERSATION_DETAIL_QUERY_KEY,
  CONVERSATIONS_QUERY_KEY,
} from "@/hooks/useConversations";
import type { MessageFeedback, MessageFeedbackRating } from "@/lib/types";

/**
 * Thumbs-up/down feedback for a single message. Loads the existing rating,
 * exposes set/clear, and tracks loading state. One rating per (user, message).
 */
export function useMessageFeedback(messageId: string | null) {
  const [feedback, setFeedbackState] = useState<MessageFeedback | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    if (!messageId) {
      setFeedbackState(null);
      return;
    }
    setIsLoading(true);
    api
      .getFeedback(messageId)
      .then((f) => {
        if (!cancelled) setFeedbackState(f);
      })
      .catch(() => {
        if (!cancelled) setFeedbackState(null);
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [messageId]);

  const set = useCallback(
    async (rating: MessageFeedbackRating, extra?: { reason?: string; comment?: string }) => {
      if (!messageId) return;
      setIsLoading(true);
      try {
        const f = await api.setFeedback(messageId, rating, extra);
        setFeedbackState(f);
      } catch (err) {
        // Surface softly instead of becoming an unhandled promise rejection
        // (the caller invokes this as `void set(...)`).
        console.warn("failed to set message feedback", err);
      } finally {
        setIsLoading(false);
      }
    },
    [messageId]
  );

  const clear = useCallback(async () => {
    if (!messageId) return;
    setIsLoading(true);
    try {
      await api.deleteFeedback(messageId);
      setFeedbackState(null);
    } catch (err) {
      console.warn("failed to clear message feedback", err);
    } finally {
      setIsLoading(false);
    }
  }, [messageId]);

  return { feedback, set, clear, isLoading };
}

/**
 * Edit-and-resend: fork a conversation at an earlier user message. The new
 * conversation copies the prior history; the caller then sends the edited text
 * to it as a fresh turn.
 */
export function useBranchConversation() {
  const queryClient = useQueryClient();
  return useCallback(
    async (conversationId: string, messageId: string, newContent: string) => {
      const branch = await api.branchConversation(conversationId, messageId, newContent);
      await queryClient.invalidateQueries({ queryKey: CONVERSATIONS_QUERY_KEY });
      await queryClient.prefetchQuery({
        queryKey: CONVERSATION_DETAIL_QUERY_KEY(branch.id),
        queryFn: () => Promise.resolve(branch),
      });
      return branch;
    },
    [queryClient]
  );
}
