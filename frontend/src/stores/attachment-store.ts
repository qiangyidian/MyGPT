"use client";

import { create } from "zustand";
import type { AttachmentRef } from "@/lib/types";

/** A draft attachment in the composer (pre-send) or restored on a message. */
export interface AttachmentDraft extends AttachmentRef {
  /** Transient upload/parse progress hint for the composer tray. */
  uploading?: boolean;
  error?: string | null;
}

/**
 * Stable empty array returned whenever a conversation has no drafts.
 *
 * IMPORTANT: zustand v5 subscribes through React's `useSyncExternalStore`,
 * which compares selector results by reference (`Object.is`) after every
 * commit. Returning a fresh `[]` literal from a selector (e.g.
 * `s.drafts[id] ?? []`) makes that comparison fail on every snapshot check,
 * so React concludes the store mutated and re-renders forever — surfacing as
 * "Maximum update depth exceeded". Returning this shared constant keeps the
 * reference stable and breaks that loop.
 */
export const EMPTY_DRAFTS: AttachmentDraft[] = [];

interface AttachmentState {
  /** Draft attachments per conversation (the composer tray before send). */
  drafts: Record<string, AttachmentDraft[]>;
  getDrafts: (conversationId: string | null) => AttachmentDraft[];
  addDraft: (conversationId: string, draft: AttachmentDraft) => void;
  updateDraft: (conversationId: string, id: string, patch: Partial<AttachmentDraft>) => void;
  removeDraft: (conversationId: string, id: string) => void;
  clearDrafts: (conversationId: string) => void;
}

export const useAttachmentStore = create<AttachmentState>((set, get) => ({
  drafts: {},
  getDrafts: (conversationId) =>
    conversationId ? get().drafts[conversationId] ?? [] : [],
  addDraft: (conversationId, draft) =>
    set((s) => ({
      drafts: {
        ...s.drafts,
        [conversationId]: [...(s.drafts[conversationId] ?? []), draft],
      },
    })),
  updateDraft: (conversationId, id, patch) =>
    set((s) => {
      const list = s.drafts[conversationId] ?? [];
      return {
        drafts: {
          ...s.drafts,
          [conversationId]: list.map((d) => (d.id === id ? { ...d, ...patch } : d)),
        },
      };
    }),
  removeDraft: (conversationId, id) =>
    set((s) => {
      const list = s.drafts[conversationId] ?? [];
      return {
        drafts: {
          ...s.drafts,
          [conversationId]: list.filter((d) => d.id !== id),
        },
      };
    }),
  clearDrafts: (conversationId) =>
    set((s) => {
      const next = { ...s.drafts };
      delete next[conversationId];
      return { drafts: next };
    }),
}));
