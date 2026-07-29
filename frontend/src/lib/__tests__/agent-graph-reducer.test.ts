// Pure-reducer unit tests for the multi-agent graph state. No React, no store.
// Mirrors spec section 十九 "前端至少增加 reducer 或状态转换测试".

import { describe, expect, it } from "vitest";
import {
  AgentGraphState,
  AgentGraphNode,
} from "@/lib/agent-graph-types";
import { reducer, emptyGraph } from "@/lib/agent-graph-reducer";

const NODE = (over: Partial<AgentGraphNode>): AgentGraphNode => ({
  id: "x",
  name: "X",
  role: "",
  status: "pending",
  stage: 0,
  lane: 0,
  ...over,
});

function graph(nodes: AgentGraphNode[], edges: AgentGraphState["edges"] = [], runId = "r1"): AgentGraphState {
  return {
    runId,
    runtime: "crewai",
    flowName: "deep_research",
    mode: "sequential",
    status: "pending",
    nodes,
    edges,
    activeAgentIds: [],
  };
}

const SERIAL_NODES: AgentGraphNode[] = [
  NODE({ id: "researcher", name: "Researcher", stage: 0 }),
  NODE({ id: "analyst", name: "Analyst", stage: 1 }),
  NODE({ id: "writer", name: "Writer", stage: 2 }),
];
const SERIAL_EDGES = [
  { id: "r-a", source: "researcher", target: "analyst", type: "handoff" as const, status: "pending" as const },
  { id: "a-w", source: "analyst", target: "writer", type: "handoff" as const, status: "pending" as const },
];

function init(runId = "r1"): AgentGraphState {
  return reducer(emptyGraph(), {
    type: "GRAPH_INITIALIZED",
    runId,
    graph: graph(SERIAL_NODES, SERIAL_EDGES, runId),
  });
}

describe("agent-graph-reducer", () => {
  it("GRAPH_INITIALIZED builds nodes + edges + active set", () => {
    const s = init();
    expect(s.nodes.map((n) => n.id)).toEqual(["researcher", "analyst", "writer"]);
    expect(s.edges).toHaveLength(2);
    expect(s.activeAgentIds).toEqual([]);
  });

  it("two AGENT_STATUS running yield activeAgentIds of length 2 (parallel)", () => {
    const nodes: AgentGraphNode[] = [
      NODE({ id: "web", stage: 1, lane: 0 }),
      NODE({ id: "kb", stage: 1, lane: 1 }),
    ];
    const s = reducer(emptyGraph(), { type: "GRAPH_INITIALIZED", runId: "r", graph: graph(nodes, [], "r") });
    const s1 = reducer(s, { type: "AGENT_STATUS", runId: "r", agentId: "web", patch: { status: "running" } });
    const s2 = reducer(s1, { type: "AGENT_STATUS", runId: "r", agentId: "kb", patch: { status: "running" } });
    expect(s2.activeAgentIds).toHaveLength(2);
    expect(new Set(s2.activeAgentIds)).toEqual(new Set(["web", "kb"]));
  });

  it("completing one parallel agent leaves the other running", () => {
    const nodes: AgentGraphNode[] = [
      NODE({ id: "web", stage: 1, status: "running" }),
      NODE({ id: "kb", stage: 1, status: "running" }),
    ];
    const s0 = reducer(emptyGraph(), { type: "GRAPH_INITIALIZED", runId: "r", graph: graph(nodes, [], "r") });
    const s1 = reducer(s0, { type: "AGENT_STATUS", runId: "r", agentId: "web", patch: { status: "completed" } });
    expect(s1.activeAgentIds).toEqual(["kb"]);
    expect(s1.nodes.find((n) => n.id === "web")!.status).toBe("completed");
    expect(s1.nodes.find((n) => n.id === "kb")!.status).toBe("running");
  });

  it("a late running event cannot regress a completed node", () => {
    const s0 = init();
    const s1 = reducer(s0, { type: "AGENT_STATUS", runId: "r1", agentId: "researcher", patch: { status: "completed", outputSummary: "done" } });
    const s2 = reducer(s1, { type: "AGENT_STATUS", runId: "r1", agentId: "researcher", patch: { status: "running" } });
    expect(s2.nodes.find((n) => n.id === "researcher")!.status).toBe("completed");
    // output_summary is still applied (non-status field merges even on terminal).
    expect(s2.nodes.find((n) => n.id === "researcher")!.outputSummary).toBe("done");
  });

  it("agent_edge active/completed updates the edge status", () => {
    const s0 = init();
    const s1 = reducer(s0, { type: "EDGE_STATUS", runId: "r1", edgeId: "r-a", status: "active" });
    expect(s1.edges.find((e) => e.id === "r-a")!.status).toBe("active");
    const s2 = reducer(s1, { type: "EDGE_STATUS", runId: "r1", edgeId: "r-a", status: "completed" });
    expect(s2.edges.find((e) => e.id === "r-a")!.status).toBe("completed");
  });

  it("tool_call is attributed to the agent via agent_id", () => {
    const s0 = init();
    const s1 = reducer(s0, {
      type: "TOOL_STARTED", runId: "r1", agentId: "researcher", callId: "c1", name: "web_search", title: "q",
    });
    const node = s1.nodes.find((n) => n.id === "researcher")!;
    expect(node.currentTool).toEqual({ callId: "c1", name: "web_search", title: "q", status: "running" });
  });

  it("run completed stops the clock driver (status is terminal)", () => {
    const s0 = init();
    const s1 = reducer(s0, { type: "RUN_STATUS", runId: "r1", status: "running" });
    const s2 = reducer(s1, { type: "RUN_STATUS", runId: "r1", status: "completed" });
    expect(["completed", "failed", "cancelled"]).toContain(s2.status);
  });

  it("switching runs: old-run events don't pollute the new active run", () => {
    const sA = init("r1");
    // Simulate a new run becoming active.
    const sB = reducer(emptyGraph(), {
      type: "GRAPH_INITIALIZED",
      runId: "r2",
      graph: graph(
        [NODE({ id: "only", stage: 0 })],
        [],
        "r2",
      ),
    });
    // A late event from r1 arrives — must be ignored (r2 is active).
    const sLate = reducer(sB, { type: "AGENT_STATUS", runId: "r1", agentId: "researcher", patch: { status: "running" } });
    expect(sLate.runId).toBe("r2");
    expect(sLate.nodes.map((n) => n.id)).toEqual(["only"]);
    expect(sLate.activeAgentIds).toEqual([]);
  });
});
