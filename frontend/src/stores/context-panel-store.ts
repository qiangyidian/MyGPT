"use client";

import { create } from "zustand";
import type { Citation, ContextTab } from "@/lib/types";

/**
 * Generic right-side Context Panel state (Execution / Sources / Files / Artifact).
 *
 * Auto-open rules (see useContextPanel) may open the panel on certain events
 * (approval waiting, agent failure, citation click, attachment preview). Once
 * the user manually closes it for a run, that run is suppressed so the same
 * event does not keep reopening it; a new task clears the suppression.
 */
interface ContextPanelState {
  open: boolean;
  tab: ContextTab;
  /** Citation index to scroll to when the Sources tab opens. */
  focusSourceIndex: number | null;
  /** Attachment id to preview when the Files tab opens. */
  focusAttachmentId: string | null;
  /** Run ids the user has manually closed -> suppress auto-open for them. */
  suppressedRunIds: Set<string>;
  /** Citations for the active run (mirrored from the live stream / message). */
  sources: Citation[];

  openWith: (tab: ContextTab, opts?: { sourceIndex?: number; attachmentId?: string }) => void;
  close: (runId?: string) => void;
  setTab: (tab: ContextTab) => void;
  setFocusSource: (index: number | null) => void;
  setSources: (sources: Citation[]) => void;
  isSuppressed: (runId: string) => boolean;
  /** Called when a new task/run starts — clears stale suppression. */
  resetForNewTask: () => void;
}

export const useContextPanelStore = create<ContextPanelState>((set, get) => ({
  open: false,
  tab: "execution",
  focusSourceIndex: null,
  focusAttachmentId: null,
  suppressedRunIds: new Set(),
  sources: [],

  openWith: (tab, opts) =>
    set({
      open: true,
      tab,
      focusSourceIndex: opts?.sourceIndex ?? null,
      focusAttachmentId: opts?.attachmentId ?? null,
    }),

  close: (runId) => {
    const suppressed = new Set(get().suppressedRunIds);
    if (runId) suppressed.add(runId);
    set({ open: false, suppressedRunIds: suppressed });
  },

  setTab: (tab) => set({ tab }),

  setFocusSource: (index) => set({ focusSourceIndex: index }),

  setSources: (sources) => set({ sources }),

  isSuppressed: (runId) => get().suppressedRunIds.has(runId),

  resetForNewTask: () =>
    set({
      open: false,
      suppressedRunIds: new Set(),
      focusSourceIndex: null,
      focusAttachmentId: null,
      sources: [],
    }),
}));

