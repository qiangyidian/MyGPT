"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { streamRunEvents } from "@/lib/api";
import {
  applyEvent,
  disconnectSubscription,
  emptyRunState,
  reconnectionShouldStop,
  type DurableRunState,
} from "@/lib/run-events";
import type { DurableRunEvent } from "@/lib/types";

/**
 * Cursor persistence (per run). An in-memory map is the primary store; we also
 * mirror to localStorage so a cross-tab navigation or page refresh resumes from
 * the same point. Best-effort — never throws.
 */
const memoryCursors = new Map<string, number>();

function readCursor(runId: string): number {
  const mem = memoryCursors.get(runId);
  if (mem !== undefined) return mem;
  try {
    const raw = window.localStorage.getItem(`durable-run-cursor:${runId}`);
    if (raw) {
      const n = parseInt(raw, 10);
      if (Number.isFinite(n) && n >= 0) {
        memoryCursors.set(runId, n);
        return n;
      }
    }
  } catch {
    /* localStorage unavailable / disabled */
  }
  return 0;
}

function writeCursor(runId: string, cursor: number): void {
  const prev = memoryCursors.get(runId) ?? 0;
  if (cursor <= prev) return;
  memoryCursors.set(runId, cursor);
  try {
    window.localStorage.setItem(`durable-run-cursor:${runId}`, String(cursor));
  } catch {
    /* localStorage unavailable / quota */
  }
}

function clearCursor(runId: string): void {
  memoryCursors.delete(runId);
  try {
    window.localStorage.removeItem(`durable-run-cursor:${runId}`);
  } catch {
    /* ignore */
  }
}

export interface UseDurableAgentRunResult {
  state: DurableRunState | null;
  /** Cancel + drop all tracked state for this run (e.g. on terminal completion). */
  clear: () => void;
}

/**
 * Subscribe to the durable run-event stream for `runId` with reconnect/replay.
 *
 * The subscription is INDEPENDENT of the chat SSE: closing it (navigate away,
 * network drop) does NOT flip `runStatus` — the workflow keeps running
 * server-side, and a reconnect replays events from the persisted cursor
 * (via `Last-Event-ID`). Reconnect uses exponential backoff (capped at 30s)
 * and stops once the run reaches a terminal status.
 *
 * Pass `null` to tear down (e.g. when no run is active).
 */
export function useDurableAgentRun(
  runId: string | null,
): UseDurableAgentRunResult {
  const [state, setState] = useState<DurableRunState | null>(
    () => (runId ? { ...emptyRunState(runId), cursor: readCursor(runId) } : null),
  );

  // Refs so the async loop reads fresh values without stale closures.
  const stateRef = useRef<DurableRunState | null>(state);
  stateRef.current = state;
  const cursorRef = useRef<number>(runId ? readCursor(runId) : 0);
  const abortRef = useRef<AbortController | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptRef = useRef(0);
  // Guards against reconnecting after unmount or after a runId switch.
  const generationRef = useRef(0);

  const clear = useCallback(() => {
    abortRef.current?.abort();
    if (reconnectTimer.current) {
      clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }
    const id = stateRef.current?.runId;
    if (id) clearCursor(id);
    setState(null);
    stateRef.current = null;
  }, []);

  useEffect(() => {
    // Tear down anything still running for the PREVIOUS runId.
    generationRef.current += 1;
    const myGeneration = generationRef.current;
    abortRef.current?.abort();
    if (reconnectTimer.current) {
      clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }

    if (!runId) {
      setState(null);
      stateRef.current = null;
      return;
    }

    // Seed state from the persisted cursor (replay picks up where we left off).
    const initialCursor = readCursor(runId);
    cursorRef.current = initialCursor;
    attemptRef.current = 0;
    const fresh = { ...emptyRunState(runId), cursor: initialCursor };
    setState(fresh);
    stateRef.current = fresh;

    const connect = () => {
      if (generationRef.current !== myGeneration) return;
      // Stop reconnecting once the workflow has ended.
      const latest = stateRef.current;
      if (latest && reconnectionShouldStop(latest)) {
        setState((s) => (s ? { ...s, subscriptionStatus: "disconnected" } : s));
        return;
      }

      const controller = new AbortController();
      abortRef.current = controller;
      setState((s) =>
        s
          ? {
              ...s,
              subscriptionStatus:
                attemptRef.current === 0 ? "connecting" : "reconnecting",
            }
          : s,
      );

      void streamRunEvents(
        runId,
        {
          onEvent: ({ sequence, event_type, data, id: frameId }) => {
            if (sequence < 0) return; // no sequence on the frame or payload
            const evt: DurableRunEvent = {
              id: frameId ?? `${runId}:${sequence}`,
              run_id: runId,
              sequence,
              event_type,
              data,
              created_at: new Date().toISOString(),
            };
            setState((s) => (s ? applyEvent(s, evt) : s));
            if (sequence > cursorRef.current) {
              cursorRef.current = sequence;
              writeCursor(runId, sequence);
            }
          },
          onCursor: (c) => {
            if (c > cursorRef.current) {
              cursorRef.current = c;
              writeCursor(runId, c);
            }
          },
          onDisconnect: () => {
            if (generationRef.current !== myGeneration) return;
            setState((s) => (s ? disconnectSubscription(s) : s));
            scheduleReconnect();
          },
          onError: ({ message }) => {
            if (generationRef.current !== myGeneration) return;
            setState((s) =>
              s
                ? {
                    ...s,
                    error: message,
                    subscriptionStatus: "disconnected",
                  }
                : s,
            );
            scheduleReconnect();
          },
        },
        { signal: controller.signal, lastEventId: cursorRef.current },
      );
    };

    const scheduleReconnect = () => {
      if (generationRef.current !== myGeneration) return;
      const latest = stateRef.current;
      if (latest && reconnectionShouldStop(latest)) return;
      attemptRef.current += 1;
      // Exponential backoff: 1s, 2s, 4s, 8s, 16s, capped at 30s.
      const backoff = Math.min(
        30_000,
        1000 * 2 ** Math.min(attemptRef.current - 1, 5),
      );
      reconnectTimer.current = setTimeout(() => {
        reconnectTimer.current = null;
        connect();
      }, backoff);
    };

    connect();

    return () => {
      // On unmount or runId switch: abort + cancel pending reconnect, but do
      // NOT flip runStatus — the workflow continues server-side and a future
      // mount resumes from the persisted cursor.
      generationRef.current += 1;
      abortRef.current?.abort();
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current);
        reconnectTimer.current = null;
      }
    };
  }, [runId]);

  return { state, clear };
}
