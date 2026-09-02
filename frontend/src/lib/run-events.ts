/**
 * Cursor-aware replay contract for the durable run-event stream (Task 12).
 *
 * The backend's `GET /api/agent-runs/{run_id}/events` is a cursor-replay SSE:
 * it replays durable `RunEvent` rows from a `Last-Event-ID` cursor, then tails
 * new events. Each frame carries `id: <sequence>` so a reconnect resumes
 * exactly where the last subscription left off.
 *
 * This module is the PURE reducer contract — no React, no network. The
 * `useDurableAgentRun` hook drives it.
 *
 * Design invariant: **subscription status ≠ workflow status.** A client
 * disconnect flips `subscriptionStatus` but never touches `runStatus` — the
 * run keeps executing on the worker, and a reconnect replays from the cursor.
 */

import type {
  DurableRunEvent,
  DurableRunStatus,
  RunSubscriptionStatus,
} from "./types";

/** Re-export so callers can import the contract + types together. */
export type { DurableRunEvent, DurableRunStatus, RunSubscriptionStatus } from "./types";

/** Workflow statuses that mark the end of a run's lifecycle. */
export const TERMINAL_RUN_STATUSES: ReadonlySet<DurableRunStatus> = new Set([
  "completed",
  "failed",
  "cancelled",
]);

/**
 * The durable run-event reducer state. `runStatus` is workflow state (survives
 * a disconnect); `subscriptionStatus` is connection state (independent).
 */
export interface DurableRunState {
  runId: string;
  runStatus: DurableRunStatus;
  subscriptionStatus: RunSubscriptionStatus;
  /** Highest event sequence observed — the `Last-Event-ID` resume cursor. */
  cursor: number;
  /** Events past the cursor, deduped by `(run_id, sequence)`, ordered. */
  events: DurableRunEvent[];
  /** Workflow paused (run.paused / run_paused). */
  paused: boolean;
  /** Blocked on a human action (approval_required / plan awaiting confirm). */
  blocked: boolean;
  /** Last surfaced error message (run.failed / error / recovery). */
  error: string | null;
}

/** A fresh state for `runId` with no events observed. */
export function emptyRunState(runId: string): DurableRunState {
  return {
    runId,
    runStatus: "pending",
    subscriptionStatus: "idle",
    cursor: 0,
    events: [],
    paused: false,
    blocked: false,
    error: null,
  };
}

/** Key used to dedupe durable events (per-run sequence is unique server-side). */
function eventKey(e: DurableRunEvent): string {
  return `${e.run_id}#${e.sequence}`;
}

/**
 * Dedupe a batch of events by `(run_id, sequence)`, keeping the LATEST copy
 * (last-write-wins) and ordering ascending by sequence. The SSE replay can
 * deliver the same sequence twice across reconnects; this collapses them.
 */
export function reduceEvents(events: DurableRunEvent[]): DurableRunEvent[] {
  const byKey = new Map<string, DurableRunEvent>();
  for (const e of events) byKey.set(eventKey(e), e);
  return Array.from(byKey.values()).sort((a, b) => a.sequence - b.sequence);
}

/** Map a durable/chat event_type to a workflow status transition. */
function transitionFromEvent(
  eventType: string,
  data: Record<string, unknown>,
): Partial<Pick<DurableRunState, "runStatus" | "paused" | "blocked" | "error">> {
  switch (eventType) {
    // Durable workflow event names (dotted).
    case "run.started":
      return { runStatus: "running" };
    case "run.completed":
      return { runStatus: "completed" };
    case "run.failed":
      return {
        runStatus: "failed",
        error: typeof data.message === "string" ? data.message : null,
      };
    case "run.cancelled":
      return { runStatus: "cancelled" };
    case "run.paused":
      return { runStatus: "paused", paused: true };
    case "run.resumed":
      return { runStatus: "running", paused: false };
    // Chat-stream event names (underscored) — same endpoint serves both.
    case "run_started":
    case "run_resumed":
      return { runStatus: "running", paused: false };
    case "run_paused":
      return { runStatus: "paused", paused: true };
    case "run_status": {
      const st = typeof data.status === "string" ? data.status : "";
      if (st === "running") return { runStatus: "running" };
      if (st === "paused") return { runStatus: "paused", paused: true };
      if (st === "completed") return { runStatus: "completed" };
      if (st === "failed") return { runStatus: "failed" };
      if (st === "cancelled") return { runStatus: "cancelled" };
      return {};
    }
    case "done": {
      // The chat `done` event carries a finish_reason; only terminal-ish
      // reasons flip to completed here (the SSE stream is ending regardless).
      const fr = typeof data.finish_reason === "string" ? data.finish_reason : "";
      if (fr === "cancelled") return { runStatus: "cancelled" };
      if (fr === "error" || fr === "provider_error" || fr === "timeout")
        return { runStatus: "failed" };
      return { runStatus: "completed" };
    }
    case "error":
      return {
        runStatus: "failed",
        error: typeof data.message === "string" ? data.message : null,
      };
    case "approval_required":
      return { blocked: true };
    default:
      return {};
  }
}

/**
 * Apply ONE new event (sequence > cursor) to the state. Returns the same
 * state reference (no copy) when the event is from a foreign run or already
 * at/below the cursor — so callers can detect "nothing changed".
 */
export function applyEvent(
  state: DurableRunState,
  event: DurableRunEvent,
): DurableRunState {
  if (event.run_id !== state.runId) return state;
  if (event.sequence <= state.cursor) return state;

  const next: DurableRunState = {
    ...state,
    events: [...state.events, event],
    cursor: event.sequence,
  };
  const patch = transitionFromEvent(event.event_type, event.data);
  if (patch.runStatus !== undefined) next.runStatus = patch.runStatus;
  if (patch.paused !== undefined) next.paused = patch.paused;
  if (patch.blocked !== undefined) next.blocked = patch.blocked;
  if (patch.error !== undefined) next.error = patch.error;
  // A non-terminal event clears the blocked flag once work resumes.
  if (
    patch.runStatus === "running" &&
    event.event_type !== "approval_required"
  ) {
    next.blocked = false;
  }
  return next;
}

/**
 * Mark the SSE subscription as disconnected WITHOUT touching the workflow
 * status. A client disconnect (navigate away, network drop) leaves the run
 * exactly where it was — the workflow continues server-side, and a reconnect
 * replays from the persisted cursor.
 */
export function disconnectSubscription(
  state: DurableRunState,
): DurableRunState {
  return { ...state, subscriptionStatus: "disconnected" };
}

/** True when the reconnection loop should stop (run reached a terminal state). */
export function reconnectionShouldStop(state: DurableRunState): boolean {
  return TERMINAL_RUN_STATUSES.has(state.runStatus);
}
