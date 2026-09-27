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

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import {
  jitteredDelayMs,
  backoffDelayMs,
  POLL_STOPPED_MESSAGE,
  RUN_POLL,
  shouldStopPolling,
} from "@/lib/poll-policy";
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
  //
  // 固定 4 秒、永不停歇的轮询在服务端出错时最要命：每个客户端都在同一节奏上重试，
  // 把还没倒的接口按得更死。所以这里换成「失败即退避、退避有上限、连续失败到次数
  // 就停、页面在后台不发请求」——并且停止时要能被用户手动重启。
  const needsPoll =
    active.runId !== "" && ["running", "waiting_approval", "pending"].includes(active.status);
  const activeRunRef = useRef(active.runId);
  activeRunRef.current = active.runId;
  const failuresRef = useRef(0);
  const [pollStopped, setPollStopped] = useState(false);

  // 换一条 run 不继承上一条的失败记账。
  useEffect(() => {
    failuresRef.current = 0;
    setPollStopped(false);
  }, [active.runId]);

  useEffect(() => {
    if (!needsPoll || pollStopped) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    const arm = () => {
      const delay = jitteredDelayMs(
        backoffDelayMs(failuresRef.current, RUN_POLL),
        Math.random
      );
      timer = setTimeout(() => {
        if (cancelled) return;
        // 后台标签页：这一轮不发也不续期，交给 visibilitychange 唤醒。
        if (typeof document !== "undefined" && document.hidden) return;
        void poll();
      }, delay);
    };

    const poll = async () => {
      if (cancelled) return;
      const ok = await restoreAgentGraph(activeRunRef.current, false);
      if (cancelled) return;
      failuresRef.current = ok ? 0 : failuresRef.current + 1;
      if (shouldStopPolling(failuresRef.current, RUN_POLL)) {
        setPollStopped(true);
        return;
      }
      arm();
    };

    const wake = () => {
      if (typeof document !== "undefined" && document.hidden) return;
      if (timer) clearTimeout(timer);
      void poll();
    };

    arm();
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", wake);
    }
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", wake);
      }
    };
  }, [needsPoll, pollStopped]);

  /** 放弃后重新开始：失败清零、回到基准间隔。 */
  const restartPoll = useCallback(() => {
    failuresRef.current = 0;
    setPollStopped(false);
  }, []);

  return { restore, pollStopped, pollStoppedMessage: POLL_STOPPED_MESSAGE, restartPoll };
}
