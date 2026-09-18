// 富步骤事件的真实回路：后端 SSE 载荷（snake_case）→ dispatchChatStreamEvent
// 的 camelCase 映射 → reducer。
//
// 这条测试覆盖的是 api.ts 的字段映射表本身 —— 后端把 agent_id 改名或映射写错
// 时，面板会静默丢失产出/进度，而单测 reducer 是发现不了的（reducer 只看到已
// 映射好的 camelCase）。
import { describe, expect, it } from "vitest";

import { dispatchChatStreamEvent, type ChatStreamHandlers } from "@/lib/api";
import { emptyGraph, reducer } from "@/lib/agent-graph-reducer";
import type { AgentGraphAction } from "@/lib/agent-graph-reducer";
import type { AgentGraphNode, AgentGraphState } from "@/lib/agent-graph-types";

const RUN = "r1";

function baseGraph(): AgentGraphState {
  return {
    ...emptyGraph(),
    runId: RUN,
    status: "running",
    nodes: [
      {
        id: "researcher",
        name: "Researcher",
        role: "资料检索",
        stage: 0,
        status: "running",
      } as AgentGraphNode,
    ],
    activeAgentIds: ["researcher"],
  };
}

/** Wire the real handler table to a reducer, capturing the actions api.ts emits. */
function runThroughHandlers(frames: Array<[string, string]>): AgentGraphState {
  const actions: AgentGraphAction[] = [];
  const capture =
    (fn: (e: any) => AgentGraphAction) =>
    (e: any) =>
      actions.push(fn(e));

  const handlers: ChatStreamHandlers = {
    onStepOutput: capture((e) => ({
      type: "STEP_OUTPUT",
      runId: e.runId,
      agentId: e.agentId,
      text: e.text,
      truncated: e.truncated,
      chars: e.chars,
    })),
    onStepProgress: capture((e) => ({
      type: "STEP_PROGRESS",
      runId: e.runId,
      agentId: e.agentId,
      elapsedS: e.elapsedS,
      note: e.note,
    })),
    onAgentStatus: capture((e) => ({
      type: "AGENT_STATUS",
      runId: e.runId,
      agentId: e.agentId,
      patch: {
        status: e.status,
        outputSummary: e.outputSummary,
        usage: e.usage,
        costUsd: e.costUsd,
      },
    })),
  };

  for (const [name, payload] of frames) {
    dispatchChatStreamEvent(handlers, name, payload);
  }
  return actions.reduce(reducer, baseGraph());
}

describe("step_output 回路", () => {
  it("snake_case 载荷经 api.ts 映射后落到节点 outputFull", () => {
    const state = runThroughHandlers([
      [
        "step_output",
        JSON.stringify({
          run_id: RUN,
          agent_id: "researcher",
          text: "检索到的完整证据",
          truncated: false,
          chars: 8,
        }),
      ],
    ]);
    expect(state.nodes[0].outputFull).toBe("检索到的完整证据");
    expect(state.nodes[0].outputTruncated).toBe(false);
  });

  it("带 truncated 的载荷被标记", () => {
    const state = runThroughHandlers([
      [
        "step_output",
        JSON.stringify({
          run_id: RUN,
          agent_id: "researcher",
          text: "x",
          truncated: true,
          chars: 25000,
        }),
      ],
    ]);
    expect(state.nodes[0].outputTruncated).toBe(true);
  });

  it("格式错误的 JSON 被丢弃而不抛错", () => {
    expect(() =>
      runThroughHandlers([["step_output", "{not json"]])
    ).not.toThrow();
  });
});

describe("step_progress 回路", () => {
  it("snake_case 载荷经映射后落到节点 progressNote", () => {
    const state = runThroughHandlers([
      [
        "step_progress",
        JSON.stringify({
          run_id: RUN,
          agent_id: "researcher",
          elapsed_s: 23,
          note: "最近工具：web_search",
        }),
      ],
    ]);
    expect(state.nodes[0].progressNote).toBe("最近工具：web_search");
  });
});

describe("agent_status 的 usage 扩展回路", () => {
  it("usage / cost_usd 经映射后落到节点", () => {
    const state = runThroughHandlers([
      [
        "agent_status",
        JSON.stringify({
          run_id: RUN,
          agent_id: "researcher",
          status: "completed",
          output_summary: "摘要",
          usage: { total_tokens: 42 },
          cost_usd: 0.03,
        }),
      ],
    ]);
    expect(state.nodes[0].status).toBe("completed");
    expect(state.nodes[0].usage).toEqual({ total_tokens: 42 });
    expect(state.nodes[0].costUsd).toBe(0.03);
  });

  it("老后端（无 usage 字段）不产生 undefined 覆盖", () => {
    const state = runThroughHandlers([
      [
        "agent_status",
        JSON.stringify({
          run_id: RUN,
          agent_id: "researcher",
          status: "running",
        }),
      ],
    ]);
    expect(state.nodes[0].usage).toBeUndefined();
    expect(state.nodes[0].costUsd).toBeUndefined();
  });
});
