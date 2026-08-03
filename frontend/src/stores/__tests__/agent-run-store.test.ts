import { describe, it, expect } from "vitest";

import {
  selectHasAgentRun,
  selectIsMultiAgent,
  selectRunningCount,
  useAgentRunStore,
} from "@/stores/agent-run-store";
import { emptyGraph } from "@/lib/agent-graph-reducer";
import type { AgentGraphNode, AgentGraphState } from "@/lib/agent-graph-types";

function graph(n: number, status: AgentGraphState["status"] = "running"): AgentGraphState {
  const nodes: AgentGraphNode[] = Array.from({ length: n }, (_, i) => ({
    id: `a${i}`,
    name: `A${i}`,
    role: "",
    status: "running",
    stage: 0,
    lane: i,
  }));
  return {
    ...emptyGraph(),
    runId: "r1",
    nodes,
    activeAgentIds: nodes.map((x) => x.id),
    status,
  };
}

describe("agent-run-store selectors (scope C: single-node visibility)", () => {
  it("selectHasAgentRun is true for a single-node run, false when there are no nodes", () => {
    useAgentRunStore.setState({ active: graph(1) });
    expect(selectHasAgentRun(useAgentRunStore.getState())).toBe(true);

    useAgentRunStore.setState({ active: { ...emptyGraph(), runId: "r1" } });
    expect(selectHasAgentRun(useAgentRunStore.getState())).toBe(false);
  });

  it("selectIsMultiAgent distinguishes single (1) vs multi (≥2)", () => {
    useAgentRunStore.setState({ active: graph(1) });
    expect(selectIsMultiAgent(useAgentRunStore.getState())).toBe(false);
    useAgentRunStore.setState({ active: graph(3) });
    expect(selectIsMultiAgent(useAgentRunStore.getState())).toBe(true);
  });

  it("selectRunningCount reads activeAgentIds", () => {
    useAgentRunStore.setState({ active: graph(2) });
    expect(selectRunningCount(useAgentRunStore.getState())).toBe(2);
  });

  it("selectHasAgentRun hides a finished+dismissed single-agent run", () => {
    useAgentRunStore.setState({
      active: graph(1, "completed"),
      dismissedRunIds: new Set(["r1"]),
    });
    expect(selectHasAgentRun(useAgentRunStore.getState())).toBe(false);
  });
});
