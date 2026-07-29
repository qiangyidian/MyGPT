"use client";

// Restore + poll + shared-clock for the multi-agent panel.
//
// The SSE → store bridge lives in useChatStream (it owns the connection and
// dispatches agent_graph/agent_status/agent_edge/run_status/tool_* directly to
// the store). This hook handles the *non-stream* concerns:
//
//   1. Restore after a page refresh / from a history bubble: given a runId,
//      GET /api/agent-runs/{id} and seed the store from its persisted graph.
//   2. Low-frequency poll fallback (4s) while a run is running or waiting on
//      approval, so a dropped SSE connection still converges to the true final
//      state. Finished runs are never polled.
//   3. A shared 1Hz clock (store.tick) that drives every live duration display
//      in node cards — one interval for the whole panel, not one per node.

import { useCallback, useEffect, useRef } from "react";
import { api } from "@/lib/api";
import type {
  AgentGraphEdge,
  AgentGraphNode,
  AgentGraphState,
  EdgeType,
} from "@/lib/agent-graph-types";
import { useAgentRunStore } from "@/stores/agent-run-store";

/** Coerce the backend graph dict (unknown) into the typed state. */
export function coerceGraph(runId: string, raw: unknown): AgentGraphState | null {
  if (!raw || typeof raw !== "object") return null;
  const g = raw as Record<string, unknown>;
  const nodes = (g.nodes as AgentGraphNode[]) ?? [];
  const edges = (g.edges as AgentGraphEdge[]) ?? [];
  return {
    runId,
    runtime: (g.runtime as "native" | "crewai") ?? "crewai",
    flowName: (g.flow_name as string) ?? (g.flowName as string) ?? "",
    mode: (g.mode as AgentGraphState["mode"]) ?? "sequential",
    status: (g.status as AgentGraphState["status"]) ?? "pending",
    nodes,
    edges,
    activeAgentIds: (g.active_agent_ids as string[]) ?? (g.activeAgentIds as string[]) ?? [],
    startedAt: g.started_at as string | undefined,
    finishedAt: g.finished_at as string | undefined,
  };
}

const TERMINAL = ["completed", "failed", "cancelled"];

/** Standalone restore (callable outside React, e.g. from a message-bubble
 *  "查看执行过程" entry). Loads the persisted graph and seeds the store. */
export async function restoreAgentGraph(runId: string, reopen: boolean = true): Promise<boolean> {
  try {
    const run = await api.getAgentRun(runId);
    if (run.graph) {
      const graph = coerceGraph(runId, run.graph);
      if (graph) {
        const store = useAgentRunStore.getState();
        store.setActiveRun(runId);
        // Only a user-initiated "查看执行过程" should clear a manual dismissal;
        // the background poll must NOT reopen a panel the user closed.
        if (reopen) store.reopenActive();
        store.dispatch({ type: "RUN_RESTORED", runId, graph });
        return true;
      }
    }
  } catch {
    // not found / network — leave the store empty so ResearchSteps can render.
  }
  return false;
}

export function useAgentRunGraph() {
  const tick = useAgentRunStore((s) => s.tick);
  const active = useAgentRunStore((s) => s.active);

  // ---- restore from API ----
  const restore = useCallback(async (runId: string) => {
    await restoreAgentGraph(runId);
  }, []);

  // ---- shared 1Hz clock while a run is active + non-terminal ----
  const running = active.runId !== "" && !TERMINAL.includes(active.status);
  const clockRef = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => {
    if (running && !clockRef.current) {
      clockRef.current = setInterval(() => tick(), 1000);
    } else if (!running && clockRef.current) {
      clearInterval(clockRef.current);
      clockRef.current = null;
    }
    return () => {
      if (clockRef.current) {
        clearInterval(clockRef.current);
        clockRef.current = null;
      }
    };
  }, [running, tick]);

  // ---- low-frequency poll fallback for running / waiting runs ----
  const needsPoll =
    active.runId !== "" && ["running", "waiting_approval", "pending"].includes(active.status);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const activeRunRef = useRef(active.runId);
  activeRunRef.current = active.runId;
  useEffect(() => {
    if (needsPoll && !pollRef.current) {
      pollRef.current = setInterval(() => {
        // Poll only refreshes graph state; it must not reopen a dismissed panel.
        if (activeRunRef.current) void restoreAgentGraph(activeRunRef.current, false);
      }, 4000);
    } else if (!needsPoll && pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [needsPoll, restore]);

  return { restore };
}
