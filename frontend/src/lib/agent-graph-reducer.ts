// Pure reducer for one agent-run graph. Kept framework-agnostic so it can be
// unit-tested directly. The Zustand store (agent-run-store.ts) wraps this and
// isolates state per runId.
//
// Invariants enforced here (so the UI can never be lied to, even by late /
// duplicate / out-of-order events):
//   * Ignoring events for a run that isn't the active one (caller checks runId).
//   * A terminal node (completed/failed/cancelled) never regresses to running.
//   * activeAgentIds is always recomputed from all running nodes — never a
//     single currentAgentId — so parallel agents are fully preserved.
//   * Duplicate events are idempotent.
//   * Tool calls are nested under the agent that owns them (agent_id).

import {
  AgentGraphEdge,
  AgentGraphNode,
  AgentGraphState,
  AgentNodeStatus,
  canTransitionTo,
  computeActiveAgents,
  GraphRunStatus,
} from "./agent-graph-types";

export type AgentGraphAction =
  | { type: "GRAPH_INITIALIZED"; runId: string; graph: AgentGraphState }
  | { type: "AGENT_STATUS"; runId: string; agentId: string; patch: Partial<AgentGraphNode> & { status: AgentNodeStatus } }
  | { type: "EDGE_STATUS"; runId: string; edgeId: string; status: AgentGraphEdge["status"]; label?: string }
  | { type: "RUN_STATUS"; runId: string; status: GraphRunStatus; currentAgentIds?: string[] }
  | { type: "TOOL_STARTED"; runId: string; agentId: string; callId: string; name: string; title?: string }
  | { type: "TOOL_COMPLETED"; runId: string; agentId: string; callId: string; ok: boolean }
  | { type: "APPROVAL_REQUIRED"; runId: string; agentId?: string }
  | { type: "RESET_RUN"; runId: string }
  | { type: "RUN_RESTORED"; runId: string; graph: AgentGraphState };

const EMPTY: AgentGraphState = {
  runId: "",
  runtime: "crewai",
  flowName: "",
  mode: "sequential",
  status: "pending",
  nodes: [],
  edges: [],
  activeAgentIds: [],
};

export function emptyGraph(): AgentGraphState {
  return { ...EMPTY, nodes: [], edges: [], activeAgentIds: [] };
}

export function isRunActive(state: AgentGraphState, runId: string): boolean {
  return state.runId === runId && runId !== "";
}

export function reducer(state: AgentGraphState, action: AgentGraphAction): AgentGraphState {
  // Guard: ignore events not for the active run (late events from a prior run).
  if ("runId" in action && action.runId && state.runId && action.runId !== state.runId) {
    return state;
  }

  switch (action.type) {
    case "GRAPH_INITIALIZED":
    case "RUN_RESTORED": {
      // The runId of the graph becomes the active run.
      return finalize(action.graph);
    }

    case "RESET_RUN":
      return emptyGraph();

    case "AGENT_STATUS": {
      const nodes = state.nodes.map((n) => {
        if (n.id !== action.agentId) return n;
        if (!canTransitionTo(n.status, action.patch.status)) {
          // No regression: keep the existing (more advanced) status, but still
          // allow non-status fields (e.g. output_summary on a completed node).
          return mergeNonStatus(n, action.patch);
        }
        return { ...n, ...action.patch };
      });
      return finalize({ ...state, nodes });
    }

    case "EDGE_STATUS": {
      const edges = state.edges.map((e) =>
        e.id === action.edgeId
          ? { ...e, status: action.status, ...(action.label ? { label: action.label } : {}) }
          : e
      );
      return finalize({ ...state, edges });
    }

    case "RUN_STATUS": {
      // Terminal run statuses never regress; running <-> waiting_approval is
      // free (a run legitimately pauses for approval, then resumes). The old
      // linear order trapped a run in waiting_approval forever after resuming.
      const TERMINAL: GraphRunStatus[] = ["completed", "failed", "cancelled"];
      const status = TERMINAL.includes(state.status) ? state.status : action.status;
      return finalize({ ...state, status });
    }

    case "TOOL_STARTED": {
      const nodes = state.nodes.map((n) =>
        n.id === action.agentId
          ? {
              ...n,
              currentTool: { callId: action.callId, name: action.name, title: action.title, status: "running" as const },
            }
          : n
      );
      return finalize({ ...state, nodes });
    }

    case "TOOL_COMPLETED": {
      const nodes = state.nodes.map((n) => {
        if (n.id !== action.agentId || !n.currentTool || n.currentTool.callId !== action.callId) {
          return n;
        }
        return {
          ...n,
          currentTool: { ...n.currentTool, status: (action.ok ? "completed" : "failed") as "completed" | "failed" },
        };
      });
      return finalize({ ...state, nodes });
    }

    case "APPROVAL_REQUIRED": {
      // The node (or whole run) enters a waiting-on-approval state.
      if (action.agentId) {
        const nodes = state.nodes.map((n) =>
          n.id === action.agentId && canTransitionTo(n.status, "waiting")
            ? { ...n, status: "waiting" as AgentNodeStatus }
            : n
        );
        return finalize({ ...state, nodes, status: "waiting_approval" });
      }
      return finalize({ ...state, status: "waiting_approval" });
    }

    default:
      return state;
  }
}

/** Recompute derived fields (activeAgentIds) after any mutation. */
function finalize(state: AgentGraphState): AgentGraphState {
  return { ...state, activeAgentIds: computeActiveAgents(state.nodes) };
}

function mergeNonStatus(node: AgentGraphNode, patch: Partial<AgentGraphNode>): AgentGraphNode {
  const { status: _ignored, ...rest } = patch;
  return { ...node, ...rest };
}
