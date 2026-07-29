"use client";

import { useCallback } from "react";
import { useContextPanelStore } from "@/stores/context-panel-store";
import type { ContextTab } from "@/lib/types";

/**
 * Selector + action wrapper around the Context Panel store. The auto-open
 * *rules* (approval waiting, run failed, long deep-research, citation click,
 * attachment preview) are applied in the page via effects; this hook just
 * exposes state and imperative actions to the components.
 */
export function useContextPanel() {
  const open = useContextPanelStore((s) => s.open);
  const tab = useContextPanelStore((s) => s.tab);
  const focusSourceIndex = useContextPanelStore((s) => s.focusSourceIndex);
  const focusAttachmentId = useContextPanelStore((s) => s.focusAttachmentId);

  const openWith = useContextPanelStore((s) => s.openWith);
  const close = useContextPanelStore((s) => s.close);
  const setTab = useContextPanelStore((s) => s.setTab);
  const setFocusSource = useContextPanelStore((s) => s.setFocusSource);

  const openTab = useCallback(
    (t: ContextTab, opts?: { sourceIndex?: number; attachmentId?: string; runId?: string }) => {
      openWith(t, opts);
    },
    [openWith]
  );

  return {
    open,
    tab,
    focusSourceIndex,
    focusAttachmentId,
    openTab,
    close,
    setTab,
    setFocusSource,
  };
}
