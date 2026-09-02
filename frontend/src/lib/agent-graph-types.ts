// Multi-agent graph types. Mirror of backend app/agents/graph.py — keep in sync.
// Separated from types.ts so the graph model + reducer logic is self-contained
// and independently testable.

export type AgentNodeStatus =
  | "pending"
  | "queued"
  | "running"
  | "waiting"
  | "completed"
  | "failed"
  | "cancelled";

export type AgentEdgeStatus = "pending" | "active" | "completed" | "failed";

export type EdgeType = "dependency" | "handoff" | "delegation";

export type GraphRunStatus =
  | "pending"
  | "running"
  | "waiting_approval"
  | "completed"
  | "failed"
  | "cancelled";

export interface AgentToolActivity {
  callId: string;
  name: string;
  title?: string;
  status: "running" | "completed" | "failed" | "waiting_approval";
}

export interface AgentGraphNode {
  id: string;
  name: string;
  role: string;
  description?: string;
  status: AgentNodeStatus;

  taskId?: string;
  taskTitle?: string;
  taskSummary?: string;

  stage: number;
  lane?: number;
  groupId?: string;

  startedAt?: string;
  finishedAt?: string;
  durationMs?: number;

  currentTool?: AgentToolActivity;

  outputSummary?: string;
  error?: string;
}

export interface AgentGraphEdge {
  id: string;
  source: string;
  target: string;
  type: EdgeType;
  status: AgentEdgeStatus;
  label?: string;
}

export interface AgentGraphState {
  runId: string;
  runtime: "native" | "crewai";
  flowName: string;
  mode: "sequential" | "parallel" | "hybrid";
  status: GraphRunStatus;

  nodes: AgentGraphNode[];
  edges: AgentGraphEdge[];

  activeAgentIds: string[];
  startedAt?: string;
  finishedAt?: string;

  /** The explicit runtime selection (runtime_selected SSE). Lets the panel
   *  distinguish a REAL multi-agent run from a native fallback. */
  selection?: RuntimeSelection;
}

/** Mirror of the backend RuntimeSelection / ev_runtime_selected payload. */
export interface RuntimeSelection {
  requestedRuntime: string;
  effectiveRuntime: string;
  agentProfile: string;
  multiAgentRequested: boolean;
  multiAgentExecuted: boolean;
  fallbackReason: string | null;
  /** True only when the answer came from the deterministic demo executor
   *  (canned, non-real content). The UI MUST show a persistent warning. */
  isDemo: boolean;
}

// A monotonic status rank — used to prevent regressions (a completed node can't
// be flipped back to running by a late/duplicate event). Higher = further along.
const STATUS_RANK: Record<AgentNodeStatus, number> = {
  pending: 0,
  queued: 1,
  waiting: 2,
  running: 3,
  failed: 4,
  cancelled: 5,
  completed: 6,
};

export const TERMINAL_NODE_STATUSES: ReadonlySet<AgentNodeStatus> = new Set([
  "completed",
  "failed",
  "cancelled",
]);

/** True if transitioning from -> to is a forward (or equal) move. */
export function canTransitionTo(from: AgentNodeStatus, to: AgentNodeStatus): boolean {
  // Terminal states never regress (a late "running" can't revive a completed node).
  if (TERMINAL_NODE_STATUSES.has(from)) return false;
  // "waiting" is a side state: reachable from any active state, and a waiting
  // node can resume to running or move to a terminal state.
  if (to === "waiting" || from === "waiting") return true;
  return STATUS_RANK[to] >= STATUS_RANK[from];
}

export function computeActiveAgents(nodes: AgentGraphNode[]): string[] {
  return nodes.filter((n) => n.status === "running").map((n) => n.id);
}
