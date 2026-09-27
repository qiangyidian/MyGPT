import { describe, it, expect } from "vitest";

import {
  errorFinishReason,
  initialStreamState,
  reduce,
  rebuildLastSendFromMessages,
  type StreamEvent,
  type StreamState,
} from "@/lib/chat-stream-state";
import {
  finishReasonToStatus,
  type Citation,
  type GenerationStatus,
  type Message,
} from "@/lib/types";

// 这里钉的是「用户会感知到的后果」：气泡里出现什么字、来源 tab 少了几条、半截
// 回答还在不在、要不要重试。字段名本身不是契约（`stepsSinceTextFrom` 除外 —— 页面
// 直接按它切片，见 `app/page.tsx:339`）。

const CONV = "conv-1";
type DoneEvent = Extract<StreamEvent, { kind: "done" }>;

function feed(state: StreamState, ...events: StreamEvent[]): StreamState {
  return events.reduce((s, e) => reduce(s, e), state);
}

/** 一轮常见的正常事件序列（正文由若干帧拼出来）。 */
function answered(...deltas: string[]): StreamState {
  return feed(
    initialStreamState(CONV),
    { kind: "meta", conversationId: CONV, messageId: "a-1" },
    ...deltas.map((delta): StreamEvent => ({ kind: "token", delta })),
    { kind: "done", finishReason: "stop" }
  );
}

const docCitation = (name: string): Citation => ({
  document_id: `d-${name}`,
  document_name: name,
  chunk_id: "c-1",
  chunk_index: 0,
  snippet: `${name} 的一段文字`,
  score: 0.7,
  source_type: "document",
});

const webSearchPayload = JSON.stringify({
  ok: true,
  query: "rust async",
  results: [
    { title: "Tokio", url: "https://tokio.rs/", snippet: "A runtime." },
    { title: "Async Book", url: "https://rust-lang.github.io/async-book/", snippet: "Async." },
  ],
});

const userMessage = (
  content: string,
  metadata: Record<string, unknown> = {}
): Message => ({
  id: `u-${content}`,
  conversation_id: CONV,
  role: "user",
  content,
  metadata,
  model_name: null,
  created_at: "2026-01-01T00:00:00.000Z",
});

describe("正文增量拼接", () => {
  it("多帧 token 拼成一条完整回答，入账的消息就是这一条", () => {
    const next = answered("你", "好", "，世", "界");
    expect(next.text).toBe("你好，世界");
    expect(next.commit?.content).toBe("你好，世界");
  });

  it("正文字符原样保留（不 trim、不合并空白，代码块缩进不会被吃掉）", () => {
    const next = feed(
      initialStreamState(CONV),
      { kind: "token", delta: "```python\n" },
      { kind: "token", delta: "    x = 1\n" },
      { kind: "token", delta: "```" }
    );
    expect(next.text).toBe("```python\n    x = 1\n```");
  });

  it("正文续上时吞掉上一批工具卡片，新批次才重新露出", () => {
    let s = feed(
      initialStreamState(CONV),
      { kind: "plan", summary: "计划", steps: [{ id: "p1", title: "检索" }] },
      { kind: "tool_call", id: "t1", name: "web_search", arguments: {} }
    );
    // 页面按 steps.slice(stepsSinceTextFrom) 渲染（page.tsx:339）。
    expect(s.steps.slice(s.stepsSinceTextFrom).map((x) => x.id)).toEqual(["p1", "t1"]);
    s = feed(s, { kind: "token", delta: "根据检索结果……" });
    expect(s.steps.slice(s.stepsSinceTextFrom)).toEqual([]);
    s = feed(s, { kind: "tool_call", id: "t2", name: "http_get", arguments: {} });
    expect(s.steps.slice(s.stepsSinceTextFrom).map((x) => x.id)).toEqual(["t2"]);
  });
});

describe("重复 / 乱序事件（幂等）", () => {
  it("同一个 tool_call 重发两次只留一张卡片", () => {
    const s = feed(
      initialStreamState(CONV),
      { kind: "tool_call", id: "t1", name: "web_search", arguments: {} },
      { kind: "tool_call", id: "t1", name: "web_search", arguments: {} }
    );
    expect(s.steps).toHaveLength(1);
    expect(s.steps.filter((x) => x.id === "t1")).toHaveLength(1);
  });

  it("研究计划重发（updated）不堆重复步骤", () => {
    const plan: StreamEvent = {
      kind: "plan",
      summary: "分两步",
      steps: [
        { id: "p1", title: "检索" },
        { id: "p2", title: "汇总" },
      ],
    };
    const s = feed(initialStreamState(CONV), plan, plan);
    expect(s.steps.map((x) => x.id)).toEqual(["p1", "p2"]);
    expect(s.steps[0].summary).toBe("分两步");
  });

  it("step_completed 早于 step_started：不凭空造一条没有起点的步骤", () => {
    let s = feed(initialStreamState(CONV), {
      kind: "step_completed",
      stepId: "p1",
      status: "done",
    });
    expect(s.steps).toEqual([]);
    s = feed(s, { kind: "step_started", stepId: "p1", title: "检索", type: "agent" });
    expect(s.steps[0].status).toBe("running");
  });

  it("步骤完成后再收到同一帧完成：状态不回退", () => {
    const s = feed(
      initialStreamState(CONV),
      { kind: "step_started", stepId: "p1", title: "检索", type: "agent" },
      { kind: "step_completed", stepId: "p1", status: "done" },
      { kind: "step_completed", stepId: "p1", status: "done" }
    );
    expect(s.steps).toHaveLength(1);
    expect(s.steps[0].status).toBe("done");
  });

  it("审批请求重发只有一行；未知 id 的批准帧不改状态", () => {
    const ap = {
      runId: "r1",
      approvalId: "ap-1",
      toolName: "python_exec",
      summary: "执行代码",
      riskLevel: "high",
      argumentsPreview: {},
    };
    let s = feed(
      initialStreamState(CONV),
      { kind: "approval_required", approval: ap },
      { kind: "approval_required", approval: ap }
    );
    expect(s.pendingApprovals).toHaveLength(1);
    s = feed(s, { kind: "approval_resolved", approvalId: "ap-1" });
    expect(s.pendingApprovals).toEqual([]);
    // 列表里已经没有它了，再点一次批准按钮不该让卡片抖一下。
    expect(reduce(s, { kind: "approval_resolved", approvalId: "ap-x" })).toBe(s);
  });

  it("meta 重放不会碰已产出的正文与步骤", () => {
    const s = feed(
      initialStreamState(CONV),
      { kind: "token", delta: "半句" },
      { kind: "step_started", stepId: "p1", title: "检索", type: "agent" },
      { kind: "meta", conversationId: CONV, messageId: "a-1" },
      { kind: "meta", conversationId: CONV, messageId: "a-1" }
    );
    expect(s.text).toBe("半句");
    expect(s.steps).toHaveLength(1);
    expect(s.assistantMessageId).toBe("a-1");
  });

  it("没有会话归属时，重复的终态帧也不会补出一个假归属", () => {
    const s = feed(
      initialStreamState(null),
      { kind: "token", delta: "回答" },
      { kind: "done", finishReason: "stop" },
      { kind: "done", finishReason: "stop" }
    );
    expect(s.commit).toBeNull();
    expect(s.conversationId).toBeNull();
    expect(s.status).toBe("complete");
  });

  it("web 引用按 url 去重（同一页面重发只列一次）", () => {
    const s = feed(
      initialStreamState(CONV),
      {
        kind: "citations",
        citations: [{ ...docCitation("A"), source_type: "web", url: "https://a.com/" }],
      },
      {
        kind: "citations",
        citations: [
          { ...docCitation("A 另一标题"), source_type: "web", url: "https://a.com" },
        ],
      }
    );
    expect(s.citations).toHaveLength(1);
  });
});

describe("终态：done 帧之后", () => {
  it("done 落一条消息，且带的是 meta 给的 messageId", () => {
    const s = answered("全部", "正文");
    expect(s.commit).toMatchObject({
      conversationId: CONV,
      messageId: "a-1",
      content: "全部正文",
      finishReason: "stop",
    });
    expect(s.terminated).toBe(true);
  });

  it("done 自带 messageId 时以它为准（后端可能换了 id）", () => {
    const s = feed(
      initialStreamState(CONV),
      { kind: "meta", conversationId: CONV, messageId: "a-1" },
      { kind: "token", delta: "正文" },
      { kind: "done", messageId: "a-2", finishReason: "stop" }
    );
    expect(s.commit?.messageId).toBe("a-2");
  });

  it("终态之后迟到的 token / 引用不再改写已入账的那条消息", () => {
    const s = answered("已入账");
    const commit = s.commit as NonNullable<StreamState["commit"]>;
    const later = feed(
      s,
      { kind: "token", delta: "（迟到的半句）" },
      { kind: "citations", citations: [docCitation("迟到文档")] },
      { kind: "done", finishReason: "stop" },
      { kind: "stream_end", reason: "user_stop" }
    );
    expect(later.commit).toBe(commit);
    expect(commit.content).toBe("已入账");
    expect(commit.citations).toEqual([]);
  });

  it("finish_reason 决定角标：正常收尾 vs 被截断", () => {
    const cases: Array<[DoneEvent, GenerationStatus]> = [
      [{ kind: "done", finishReason: "stop" }, "complete"],
      [{ kind: "done", finishReason: "tool_calls" }, "complete"],
      [{ kind: "done", finishReason: "length" }, "truncated"],
      [{ kind: "done", finishReason: "budget" }, "truncated"],
    ];
    for (const [event, status] of cases) {
      const s = feed(
        initialStreamState(CONV),
        { kind: "meta", conversationId: CONV, messageId: "a-1" },
        { kind: "token", delta: "正文" },
        event
      );
      expect(s.status).toBe(status);
      expect(s.finishReason).toBe(event.finishReason);
      expect(finishReasonToStatus(s.finishReason!)).toBe(status);
    }
  });

  it("error 帧的 code 决定角标：timeout / interrupted / error", () => {
    expect(errorFinishReason("provider_timeout")).toBe("timeout");
    expect(errorFinishReason("stream_disconnected")).toBe("stream_disconnected");
    expect(errorFinishReason("provider_error")).toBe("provider_error");
    expect(errorFinishReason("credits_exhausted")).toBe("error");
    expect(errorFinishReason(undefined)).toBe("error");
    const s = feed(
      initialStreamState(CONV),
      { kind: "meta", conversationId: CONV, messageId: "a-1" },
      { kind: "token", delta: "正文" },
      { kind: "error", code: "provider_timeout", message: "模型服务超时" }
    );
    expect(s.status).toBe("error");
    expect(s.commit?.finishReason).toBe("timeout");
  });
});

describe("错误信封（后端 message 是中文，可能带 code）", () => {
  it("服务端中文原文直接给用户，不被通用文案覆盖", () => {
    const s = feed(
      initialStreamState(CONV),
      {
        kind: "error",
        code: "credits_exhausted",
        message: "积分不足，请先兑换积分后继续",
        status: 402,
      }
    );
    expect(s.error).toBe("积分不足，请先兑换积分后继续");
  });

  it("没有 message 时也给出中文（toast 的 description 不能是空）", () => {
    const s = feed(initialStreamState(CONV), {
      kind: "error",
      code: "provider_rate_limited",
      status: 429,
    });
    expect(s.error).toBeTruthy();
    expect(s.error).toMatch(/[一-鿿]/);
    expect(s.error).not.toContain("provider_rate_limited");
  });

  it("pydantic 校验数组（detail）也翻成中文，不原样甩给用户", () => {
    const s = feed(
      initialStreamState(CONV),
      {
        kind: "error",
        code: "validation_error",
        status: 422,
        detail: [{ loc: ["body", "content"], msg: "Field required", type: "missing" }],
      }
    );
    expect(s.error).toContain("不能为空");
    expect(s.error).not.toContain("Field required");
  });

  it("错误前已产出的半截正文照常入账，用户不会白等一轮", () => {
    const s = feed(
      initialStreamState(CONV),
      { kind: "meta", conversationId: CONV, messageId: "a-1" },
      { kind: "token", delta: "前半段" },
      { kind: "token", delta: "（未完）" },
      { kind: "error", code: "provider_error", message: "模型服务返回错误" }
    );
    expect(s.commit?.content).toBe("前半段（未完）");
    expect(s.commit?.messageId).toBe("a-1");
    expect(s.error).toBe("模型服务返回错误");
  });

  it("一轮只入账一条错误消息：后来的错误不改口", () => {
    const s = feed(
      initialStreamState(CONV),
      { kind: "meta", conversationId: CONV, messageId: "a-1" },
      { kind: "token", delta: "正文" },
      { kind: "error", code: "provider_error", message: "第一条错误" },
      { kind: "error", code: "provider_timeout", message: "第二条错误" }
    );
    expect(s.error).toBe("第一条错误");
    expect(s.commit?.content).toBe("正文");
  });

  it("fetch 层失败（拿不到流）也算终态，并给出网络中文", () => {
    const s = feed(
      initialStreamState(CONV),
      { kind: "fetch_failed", error: new Error("Failed to fetch") }
    );
    expect(s.terminated).toBe(true);
    expect(s.status).toBe("error");
    expect(s.error).toBe("网络连接中断，请稍后重试");
    expect(s.commit).toBeNull();
  });

  it("fetch 层失败前已有正文时，半截仍然入账", () => {
    const s = feed(
      initialStreamState(CONV),
      { kind: "meta", conversationId: CONV, messageId: "a-1" },
      { kind: "token", delta: "已经写了一半" },
      { kind: "fetch_failed", error: new Error("Failed to fetch") }
    );
    expect(s.commit?.content).toBe("已经写了一半");
    expect(s.error).toBe("网络连接中断，请稍后重试");
  });

  it("done 之后的 fetch 层异常不再改口（竞态时以成功为准）", () => {
    const s = feed(
      answered("正常收尾"),
      { kind: "fetch_failed", error: new Error("Failed to fetch") }
    );
    expect(s.error).toBeNull();
    expect(s.status).toBe("complete");
  });
});

describe("取消 / 断线 / 卸载时已产出内容的去留", () => {
  it("用户点停止：半截保留为「已取消」，可继续", () => {
    const s = feed(
      initialStreamState(CONV),
      { kind: "meta", conversationId: CONV, messageId: "a-1" },
      { kind: "token", delta: "打到一半" },
      { kind: "stream_end", reason: "user_stop" }
    );
    expect(s.commit?.content).toBe("打到一半");
    expect(s.commit?.finishReason).toBe("cancelled");
    expect(s.status).toBe("cancelled");
    expect(s.error).toBeNull();
  });

  it("meta 没给 id 时，停止也补一个稳定 id（不会和上一轮撞车）", () => {
    const s = reduce(
      feed(initialStreamState(CONV), { kind: "token", delta: "半截" }),
      { kind: "stream_end", reason: "user_stop" },
      1700000000000
    );
    expect(s.commit?.messageId).toBe("cancelled-1700000000000");
  });

  it("一帧正文都没有时点停止：不入账（不留空气泡），但角标是已取消", () => {
    const s = feed(initialStreamState(CONV), { kind: "stream_end", reason: "user_stop" });
    expect(s.commit).toBeNull();
    expect(s.status).toBe("cancelled");
    expect(s.error).toBeNull();
  });

  it("切走页面（卸载式 abort）：什么都不入账，run 继续跑，回来重放", () => {
    const s = feed(
      initialStreamState(CONV),
      { kind: "meta", conversationId: CONV, messageId: "a-1" },
      { kind: "token", delta: "还没写完" },
      { kind: "stream_end", reason: "unmount" }
    );
    expect(s.commit).toBeNull();
    expect(s.terminated).toBe(false);
    expect(s.text).toBe("还没写完");
  });

  it("socket 掉了又没有终态帧：保留已生成内容并告诉用户连接中断", () => {
    const s = reduce(
      feed(
        initialStreamState(CONV),
        { kind: "meta", conversationId: CONV, messageId: "a-1" },
        { kind: "token", delta: "写了三成" }
      ),
      { kind: "stream_end", reason: "socket_closed" },
      1700000000001
    );
    expect(s.commit?.content).toBe("写了三成");
    expect(s.commit?.messageId).toBe("a-1");
    expect(s.commit?.finishReason).toBe("stream_disconnected");
    expect(s.status).toBe("interrupted");
    expect(s.error).toBe("连接中断，已保留已生成内容");
  });

  it("断线且 meta 没给 id 时用 interrupted-<时间戳> 兜底", () => {
    const s = reduce(
      feed(initialStreamState(CONV), { kind: "token", delta: "半截" }),
      { kind: "stream_end", reason: "socket_closed" },
      1700000000002
    );
    expect(s.commit?.messageId).toBe("interrupted-1700000000002");
  });

  it("现状：还没有会话归属就断线 → 不入账也不提示（用户只看到回答停了）", () => {
    const s = feed(
      initialStreamState(null),
      { kind: "token", delta: "有内容" },
      { kind: "stream_end", reason: "socket_closed" }
    );
    expect(s.commit).toBeNull();
    expect(s.error).toBeNull();
    expect(s.status).toBe("complete");
  });
});

describe("来源与终态快照簿记", () => {
  it("KB 引用与后来的网页引用并存，不是互相覆盖", () => {
    const s = feed(
      initialStreamState(CONV),
      { kind: "citations", citations: [docCitation("手册.pdf")] },
      { kind: "tool_call", id: "t1", name: "web_search", arguments: {} },
      { kind: "tool_result", id: "t1", name: "web_search", ok: true, result: webSearchPayload }
    );
    expect(s.citations.map((c) => c.document_name)).toEqual([
      "手册.pdf",
      "Tokio",
      "Async Book",
    ]);
  });

  it("工具失败时不升格来源（失败的搜索结果不该进来源 tab）", () => {
    const s = feed(
      initialStreamState(CONV),
      { kind: "tool_call", id: "t1", name: "web_search", arguments: {} },
      { kind: "tool_result", id: "t1", name: "web_search", ok: false, result: webSearchPayload }
    );
    expect(s.citations).toEqual([]);
    expect(s.steps[0].status).toBe("error");
  });

  it("api.ts 可能不带 citations 字段：不炸，也不清空已有来源", () => {
    const s = feed(
      initialStreamState(CONV),
      { kind: "citations", citations: [docCitation("手册.pdf")] },
      { kind: "citations", citations: undefined },
      { kind: "citations", citations: null }
    );
    expect(s.citations).toHaveLength(1);
  });

  it("入账消息带 run_id 与终态那一刻的步骤/引用快照", () => {
    const s = feed(
      initialStreamState(CONV),
      { kind: "meta", conversationId: CONV, messageId: "a-1" },
      { kind: "run_started", runId: "r-9" },
      { kind: "plan", summary: "两步", steps: [{ id: "p1", title: "检索" }] },
      { kind: "citations", citations: [docCitation("手册.pdf")] },
      { kind: "token", delta: "正文" },
      { kind: "done", finishReason: "stop" }
    );
    expect(s.commit?.runId).toBe("r-9");
    expect(s.commit?.steps.map((x) => x.id)).toEqual(["p1"]);
    expect(s.commit?.citations).toBe(s.citations);
  });

  it("一轮最多一个 commit：重复终态帧不会让 credits 再失效一次", () => {
    const s = answered("正文");
    const first = s.commit;
    const after = feed(
      s,
      { kind: "error", code: "provider_error", message: "后到的错误" },
      { kind: "stream_end", reason: "user_stop" },
      { kind: "stream_end", reason: "socket_closed" }
    );
    expect(after.commit).toBe(first);
  });

  it("runId 在一轮内只前进，不被后续事件清掉", () => {
    const s = feed(
      initialStreamState(CONV),
      { kind: "runtime_selected", runId: "r-1" },
      { kind: "plan", steps: [{ id: "p1", title: "检索" }] },
      { kind: "token", delta: "正文" },
      { kind: "agent_graph", runId: "r-1" }
    );
    expect(s.runId).toBe("r-1");
  });
});

describe("重发 / 回归路径", () => {
  it("刷新后回到会话：重放的回答落到原来那条 assistant 消息上", () => {
    const s = feed(
      initialStreamState(null),
      { kind: "seed", conversationId: CONV, messageId: "a-7" },
      { kind: "token", delta: "重放出来的正文" },
      { kind: "done", finishReason: "stop" }
    );
    expect(s.commit).toMatchObject({ conversationId: CONV, messageId: "a-7" });
    // durable 日志里没有 meta 帧，seed 的 id 也不能被后来的空帧冲掉。
    expect(reduce(s, { kind: "seed", messageId: null }).assistantMessageId).toBe("a-7");
  });

  it("重新生成是干净的一轮：上一轮的正文/步骤/错误都不串味", () => {
    feed(
      initialStreamState(CONV),
      { kind: "meta", conversationId: CONV, messageId: "a-1" },
      { kind: "token", delta: "上一轮" },
      { kind: "citations", citations: [docCitation("手册.pdf")] },
      { kind: "error", code: "provider_error", message: "上一轮报错" },
      { kind: "stream_end", reason: "socket_closed" }
    );
    const next = initialStreamState(CONV);
    expect(next.text).toBe("");
    expect(next.steps).toEqual([]);
    expect(next.citations).toEqual([]);
    expect(next.error).toBeNull();
    expect(next.commit).toBeNull();
    expect(next.terminated).toBe(false);
  });

  it("继续生成拿到的是最近一条用户消息的原始发送参数", () => {
    const rebuilt = rebuildLastSendFromMessages(
      [
        userMessage("第一问", {
          send_params: { mode: "speed", model_id: "m-1", knowledge_base_ids: [] },
        }),
        userMessage("勾了知识库的那问", {
          send_params: {
            mode: "expert",
            model_id: "m-2",
            knowledge_base_ids: ["kb-1"],
            mentions: [{ kind: "kb", id: "kb-1" }],
            attachment_ids: ["att-1"],
          },
        }),
        { ...userMessage("助手回答"), role: "assistant" as const },
      ],
      CONV
    );
    expect(rebuilt).toEqual({
      content: "勾了知识库的那问",
      opts: {
        conversationId: CONV,
        mode: "expert",
        modelId: "m-2",
        knowledgeBaseIds: ["kb-1"],
        mentions: [{ kind: "kb", id: "kb-1" }],
        attachmentIds: ["att-1"],
      },
    });
  });

  it("老消息没存 send_params：正文还能重发，模型退回默认（不静默失败）", () => {
    const rebuilt = rebuildLastSendFromMessages([userMessage("历史提问")], CONV);
    expect(rebuilt).toMatchObject({
      content: "历史提问",
      opts: { conversationId: CONV, modelId: null },
    });
    expect(rebuilt?.opts.mode).toBeUndefined();
  });

  it("会话里一条用户消息都没有时返回 null（页面据此提示，而不是按了没反应）", () => {
    expect(rebuildLastSendFromMessages([], CONV)).toBeNull();
    expect(
      rebuildLastSendFromMessages([{ ...userMessage("只有回答"), role: "assistant" }], CONV)
    ).toBeNull();
  });
});

describe("空流", () => {
  it("一帧 token 都没有的 done：现状仍落一条空 assistant 消息（钉住，见报告 bug）", () => {
    const s = feed(
      initialStreamState(CONV),
      { kind: "meta", conversationId: CONV, messageId: "a-1" },
      { kind: "done", finishReason: "stop" }
    );
    expect(s.commit?.content).toBe("");
    expect(s.status).toBe("complete");
  });

  it("只有引用没有正文时，引用不会凭空变成气泡内容", () => {
    const s = feed(
      initialStreamState(CONV),
      { kind: "meta", conversationId: CONV, messageId: "a-1" },
      { kind: "citations", citations: [docCitation("手册.pdf")] },
      { kind: "done", finishReason: "stop" }
    );
    expect(s.text).toBe("");
    expect(s.commit?.content).toBe("");
    expect(s.commit?.citations).toHaveLength(1);
  });

  it("初始状态本身就是「什么都没发生」：不显示错误、不显示角标", () => {
    const s = initialStreamState(CONV);
    expect(s.error).toBeNull();
    expect(s.finishReason).toBeNull();
    expect(s.status).toBe("complete");
    expect(s.stepsSinceTextFrom).toBe(0);
  });
});
