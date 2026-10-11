import { describe, expect, it } from "vitest";
import { getMessageTerminationNotice } from "@/lib/message-termination";
import type { Message } from "@/lib/types";

const message = (finish_reason: string, content = "", metadata = {}): Message => ({
  id: "m1", conversation_id: "c1", role: "assistant", content,
  metadata: { finish_reason, ...metadata }, model_name: null, created_at: "t",
});

describe("message termination recovery", () => {
  it("explains the production step-budget failure and retries the empty answer", () => {
    expect(getMessageTerminationNotice(message("budget", "", {
      budget: { reason: "max agent steps (8) reached" },
    }))).toEqual({ label: "Agent 已达到执行步数上限，尚未生成回答", action: "retry" });
  });

  it.each([
    ["max tool calls (40) reached", "工具调用次数"],
    ["max replans (2) reached", "重新规划次数"],
    ["time budget (300.0s) exceeded", "执行时间"],
    ["token budget (40000) exceeded", "总用量"],
    ["cost budget ($5) exceeded", "费用"],
  ])("distinguishes execution limits: %s", (reason, label) => {
    expect(getMessageTerminationNotice(message("budget", "部分回答", { budget: { reason } })))
      .toEqual({ label: `Agent 已达到${label}上限，已保留生成内容`, action: "continue" });
  });

  it("uses legacy error metadata or a generic budget label", () => {
    expect(getMessageTerminationNotice(message("budget", "", { error: "max agent steps (8) reached" }))?.label)
      .toContain("执行步数上限");
    expect(getMessageTerminationNotice(message("budget"))?.label).toContain("执行额度上限");
  });

  it.each(["length", "stream_disconnected", "cancelled"])("retries %s when no answer exists", (reason) => {
    expect(getMessageTerminationNotice(message(reason, " \n"))?.action).toBe("retry");
    expect(getMessageTerminationNotice(message(reason, "部分回答"))?.action).toBe("continue");
  });

  it("keeps true output truncation distinct from execution limits", () => {
    expect(getMessageTerminationNotice(message("length", "部分回答")))
      .toEqual({ label: "输出达到长度上限，内容可能不完整", action: "continue" });
    expect(getMessageTerminationNotice(message("stop", "回答"))).toBeNull();
    expect(getMessageTerminationNotice(message("provider_error", "部分回答"))?.action).toBe("retry");
  });
});
