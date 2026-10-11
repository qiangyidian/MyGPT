import { beforeAll, describe, expect, it } from "vitest";
import { renderToString } from "react-dom/server";
import * as React from "react";
import { MessageStatusBanner } from "@/components/message-bubble";
import type { Message } from "@/lib/types";

beforeAll(() => {
  (globalThis as { React?: unknown }).React = React;
});

const failed = (content: string): Message => ({
  id: "m1", conversation_id: "c1", role: "assistant", content,
  metadata: { finish_reason: "budget", budget: { reason: "max agent steps (8) reached" } },
  model_name: null, created_at: "t",
});

describe("MessageStatusBanner", () => {
  it("shows the true budget failure and regenerate button for an empty answer", () => {
    const html = renderToString(<MessageStatusBanner message={failed("")} onContinue={() => {}} onRegenerate={() => {}} />);
    expect(html).toContain("执行步数上限");
    expect(html).toContain("尚未生成回答");
    expect(html).toContain("重新生成");
    expect(html).not.toContain("继续生成");
    expect(html).not.toContain("输出达到长度上限");
  });

  it("offers continuation when a partial answer exists", () => {
    const html = renderToString(<MessageStatusBanner message={failed("部分回答")} onContinue={() => {}} onRegenerate={() => {}} />);
    expect(html).toContain("已保留生成内容");
    expect(html).toContain("继续生成");
    expect(html).not.toContain("重新生成");
  });
});
