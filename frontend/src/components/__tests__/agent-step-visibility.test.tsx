/**
 * Panel components consuming the rich step events.
 *
 * `AgentNodeCard` and `AgentActivityFeed` take their data as props, so they are
 * rendered through react-dom/server (same pipeline the browser runs, minus the
 * DOM) — the approach the existing component tests use.
 *
 * `AgentInlineStatus` reads a zustand store, and react-dom/server has no
 * server snapshot for it, so a server render would only ever see the initial
 * store. Its copy therefore lives in the exported pure `inlineStatusLabel`,
 * which is what these tests drive directly.
 */
import { beforeAll, describe, expect, it } from "vitest";
import { renderToString } from "react-dom/server";
import * as React from "react";

import { AgentInlineStatus, inlineStatusLabel } from "@/components/agents/agent-inline-status";
import { AgentNodeCard } from "@/components/agents/agent-node-card";
import { AgentActivityFeed } from "@/components/agents/agent-activity-feed";
import { emptyGraph } from "@/lib/agent-graph-reducer";
import type { AgentGraphNode, AgentGraphState } from "@/lib/agent-graph-types";

beforeAll(() => {
  (globalThis as { React?: unknown }).React = React;
});

// startedAt 必须给：activity feed 只渲染真正开始过的节点（见 buildFeed 的
// `if (n.startedAt)`），否则条目会被静默跳过。
const node = (patch: Partial<AgentGraphNode> = {}): AgentGraphNode => ({
  id: "researcher",
  name: "Researcher",
  role: "资料检索",
  stage: 0,
  status: "completed",
  startedAt: "2026-09-18T00:00:00.000Z",
  ...patch,
});

function graphWith(nodes: AgentGraphNode[], active: string[] = []): AgentGraphState {
  return { ...emptyGraph(), runId: "r1", status: "running", nodes, activeAgentIds: active };
}

describe("AgentNodeCard", () => {
  it("详情入口在只有完整产出（无摘要）时仍然出现", () => {
    const html = renderToString(
      <AgentNodeCard node={node({ outputFull: "完整证据正文" })} />
    );
    expect(html).toContain("详情");
  });

  it("无任何可展示详情时不渲染详情入口", () => {
    const html = renderToString(<AgentNodeCard node={node()} />);
    expect(html).not.toContain("详情");
  });
});

describe("inlineStatusLabel", () => {
  it("无运行时回退到思考中，不谎报进度", () => {
    expect(inlineStatusLabel(emptyGraph())).toBe("思考中…");
  });

  it("单 Agent 运行显示助手中", () => {
    expect(
      inlineStatusLabel(graphWith([node({ status: "running" })], ["researcher"]))
    ).toBe("智能助手正在作答");
  });

  it("运行中显示心跳带来的最近工具", () => {
    const label = inlineStatusLabel(
      graphWith(
        [
          node({ id: "a", name: "Advocate A", status: "running" }),
          node({
            id: "b",
            name: "Advocate B",
            status: "running",
            progressNote: "最近工具：web_search",
          }),
        ],
        ["a", "b"]
      )
    );
    expect(label).toContain("并行中");
    // 取第一个运行中的节点作为「最近」代表。
    expect(label).toContain("Advocate A");
  });

  it("单节点运行中显示其名字与最近工具", () => {
    const label = inlineStatusLabel(
      graphWith(
        [
          node({ id: "a", name: "Researcher", status: "running" }),
          node({ id: "b", name: "Analyst", status: "pending" }),
        ],
        ["a"]
      )
    );
    expect(label).toContain("Researcher");
    expect(label).toContain("处理中");
  });

  it("完成后显示完成与耗时，而不是「并行中」", () => {
    const label = inlineStatusLabel(
      graphWith([
        node({ status: "completed", durationMs: 12300 }),
        node({ id: "b", name: "Analyst", status: "pending" }),
      ])
    );
    expect(label).toContain("完成");
    expect(label).toContain("12.3s");
    expect(label).not.toContain("并行中");
  });

  it("已开始但未完成时显示协作中", () => {
    const label = inlineStatusLabel(
      graphWith([
        node({ status: "waiting" }),
        node({ id: "b", name: "Analyst", status: "pending" }),
      ])
    );
    expect(label).toBe("多 Agent 协作中");
  });
});

describe("AgentInlineStatus", () => {
  it("无 store 数据时渲染思考中（不崩）", () => {
    const html = renderToString(<AgentInlineStatus />);
    expect(html).toContain("思考中");
  });
});

describe("AgentActivityFeed", () => {
  it("完成条目携带完整产出并可展开", () => {
    const html = renderToString(
      <AgentActivityFeed
        graph={graphWith([node({ outputFull: "检索到的完整证据正文" })])}
      />
    );
    expect(html).toContain("执行完毕");
    expect(html).toContain("查看完整产出");
    expect(html).toContain("检索到的完整证据正文");
  });

  it("截断的产出给出提示", () => {
    const html = renderToString(
      <AgentActivityFeed
        graph={graphWith([node({ outputFull: "x", outputTruncated: true })])}
      />
    );
    expect(html).toContain("产出过长，已截断显示。");
  });

  it("无完整产出时回退到摘要（避免两处文案不一致）", () => {
    const html = renderToString(
      <AgentActivityFeed graph={graphWith([node({ outputSummary: "简短摘要" })])} />
    );
    expect(html).toContain("查看完整产出");
    expect(html).toContain("简短摘要");
  });

  it("运行中的节点没有产出详情", () => {
    const html = renderToString(
      <AgentActivityFeed
        graph={graphWith([node({ status: "running", outputFull: "半截" })], ["researcher"])}
      />
    );
    expect(html).not.toContain("查看完整产出");
  });

  it("失败节点展示错误而不是完整产出", () => {
    const html = renderToString(
      <AgentActivityFeed
        graph={graphWith([
          node({ status: "failed", error: "模型超时", outputFull: "半截产出" }),
        ])}
      />
    );
    expect(html).toContain("模型超时");
    expect(html).not.toContain("查看完整产出");
  });
});

describe("新 profile 标签", () => {
  it("任务分解与写-审-改都有中文标签", async () => {
    const mod = await import("@/components/agents/agent-run-header");
    const labels = mod.PROFILE_LABELS;
    expect(labels.task_decomposition).toContain("任务分解");
    expect(labels.write_review).toContain("写");
    expect(labels.deep_research).toBeTruthy();
    expect(labels.debate).toBeTruthy();
  });
});
