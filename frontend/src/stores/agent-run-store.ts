"use client";

// Per-run multi-agent graph store. Zustand keeps a single active graph (the
// run currently streaming or being viewed) plus a small cache of previously
// seen runs so re-opening a history bubble restores instantly.
//
// All mutations go through the pure reducer (agent-graph-reducer.ts), which
// enforces no-regression / idempotency / parallel-preservation. The store
// itself only handles runId isolation and the shared 1Hz "tick" that drives
// live duration displays (one interval for the whole panel, not per-node).

import { create } from "zustand";
import {
  AgentGraphState,
  RuntimeSelection,
} from "@/lib/agent-graph-types";
import {
  AgentGraphAction,
  emptyGraph,
  isRunActive,
  reducer,
} from "@/lib/agent-graph-reducer";

interface AgentRunStoreState {
  /** The currently-active graph (the run being streamed or viewed). */
  active: AgentGraphState;
  /** Cache of finished graphs by runId, for instant restore. */
  cache: Record<string, AgentGraphState>;
  /** Monotonic clock value (increments each second while anything is running). */
  clock: number;
  /** Has the user manually closed the panel for the active run? Prevents
   *  subsequent state events from re-opening it for the same run. */
  dismissedRunIds: Set<string>;

  // ---- actions ----
  dispatch: (action: AgentGraphAction) => void;
  /** Make ``runId`` the active graph (loads from cache if present). */
  setActiveRun: (runId: string) => void;
  /** Mark the active run as user-dismissed (won't auto-reopen on later events). */
  dismissActive: () => void;
  /** Reopen the panel for the active run (clears a prior dismissal). */
  reopenActive: () => void;
  /** Clear the active run (e.g. on new chat). */
  resetActive: () => void;
  /** Record the explicit runtime selection (runtime_selected SSE). */
  setRuntimeSelection: (sel: { runId: string } & RuntimeSelection) => void;
  /** Shared clock tick — called once per second by the panel while mounted. */
  tick: () => void;
}

function cacheIfTerminal(state: AgentGraphState): boolean {
  return ["completed", "failed", "cancelled"].includes(state.status);
}

export const useAgentRunStore = create<AgentRunStoreState>((set, get) => ({
  active: emptyGraph(),
  cache: {},
  clock: 0,
  dismissedRunIds: new Set(),

  dispatch: (action) =>
    set((s) => {
      const next = reducer(s.active, action);
      // Cache terminal graphs so a history bubble can reopen instantly.
      const cache = cacheIfTerminal(next)
        ? { ...s.cache, [next.runId]: next }
        : s.cache;
      return { active: next, cache };
    }),

  setActiveRun: (runId) =>
    set((s) => {
      if (s.active.runId === runId) return s;
      const cached = s.cache[runId];
      return {
        active: cached ? { ...cached } : { ...emptyGraph(), runId },
        // Clear dismissal for the newly active run only if we have real data.
      };
    }),

  dismissActive: () =>
    set((s) => {
      if (!s.active.runId) return s;
      const dismissed = new Set(s.dismissedRunIds);
      dismissed.add(s.active.runId);
      return { dismissedRunIds: dismissed };
    }),

  reopenActive: () =>
    set((s) => {
      if (!s.active.runId) return s;
      const dismissed = new Set(s.dismissedRunIds);
      dismissed.delete(s.active.runId);
      return { dismissedRunIds: dismissed };
    }),

  resetActive: () => set({ active: emptyGraph() }),

  setRuntimeSelection: (sel) =>
    set((s) => {
      // Attach to the active run; if a different run is selected, make it active
      // (it arrives right after run_started, before any graph event).
      const base =
        s.active.runId === sel.runId
          ? s.active
          : { ...emptyGraph(), runId: sel.runId };
      const next: AgentGraphState = { ...base, selection: sel };
      return { active: next };
    }),

  tick: () => set((s) => ({ clock: s.clock + 1 })),
}));

// ---- selectors ---------------------------------------------------------------
/** True if there is an active agent run with ≥1 node (multi OR single-agent).
 *  Scope C: native single-agent turns also surface — drives the trigger pill
 *  and inline status, so users perceive "an agent is working" on every turn. */
export function selectHasAgentRun(state: AgentRunStoreState): boolean {
  const { active, dismissedRunIds } = state;
  if (!active.runId) return false;
  if (active.nodes.length < 1) return false;
  // A finished+dismissed run hides the pill; a dismissed-but-running run stays
  // visible so the user can reopen it.
  if (dismissedRunIds.has(active.runId) && isRunFinished(active)) return false;
  return true;
}

/** True if the active run is a genuine multi-agent crew (≥2 nodes). */
export function selectIsMultiAgent(state: AgentRunStoreState): boolean {
  return state.active.nodes.length >= 2;
}

/** Number of agents currently running in the active graph. */
export function selectRunningCount(state: AgentRunStoreState): number {
  return state.active.activeAgentIds.length;
}

/** True if the active run is a multi-agent run (≥2 nodes) and not dismissed. */
export function selectShouldShowPanel(state: AgentRunStoreState): boolean {
  const { active, dismissedRunIds } = state;
  if (!active.runId) return false;
  if (active.nodes.length < 2) return false;
  if (dismissedRunIds.has(active.runId) && isRunFinished(active)) return false;
  // Dismissed but still running: keep closed until a NEW run starts.
  if (dismissedRunIds.has(active.runId)) return false;
  return true;
}

export function isRunFinished(g: AgentGraphState): boolean {
  return ["completed", "failed", "cancelled"].includes(g.status);
}

/** True when a multi-agent request fell back to native (no real multi-agent).
 *  The UI shows a warning banner instead of opening a fake agent panel. */
export function selectRuntimeFallback(state: AgentRunStoreState): RuntimeSelection | null {
  const sel = state.active.selection;
  if (!sel) return null;
  return sel.multiAgentRequested && !sel.multiAgentExecuted ? sel : null;
}

/** True when the active run's answer came from the deterministic demo executor
 *  (canned, non-real content). The UI MUST render a persistent warning so a
 *  demo answer is never mistaken for a genuine model reply. */
export function selectIsDemo(state: AgentRunStoreState): boolean {
  return !!state.active.selection?.isDemo;
}

/** Convenience: dispatch helper bound to a specific runId. */
export function makeDispatcher(runId: string, dispatch: (a: AgentGraphAction) => void) {
  return (a: Omit<AgentGraphAction, "runId">) =>
    dispatch({ ...(a as AgentGraphAction), runId });
}
