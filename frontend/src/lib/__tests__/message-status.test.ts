import { describe, it, expect } from "vitest";
import {
  finishReasonToStatus,
  getMessageFinishReason,
  getMessageStatus,
  isPartialResult,
  type Message,
} from "@/lib/types";

const msg = (finish_reason: unknown): Message =>
  ({
    id: "m1",
    conversation_id: "c1",
    role: "assistant",
    content: "x",
    metadata: finish_reason === undefined ? {} : { finish_reason },
    model_name: null,
    created_at: "t",
  }) as unknown as Message;

describe("finishReasonToStatus", () => {
  it("maps stop/tool_calls to complete", () => {
    expect(finishReasonToStatus("stop")).toBe("complete");
    expect(finishReasonToStatus("tool_calls")).toBe("complete");
  });
  it("maps length/budget to truncated", () => {
    expect(finishReasonToStatus("length")).toBe("truncated");
    expect(finishReasonToStatus("budget")).toBe("truncated");
  });
  it("maps cancelled + legacy aborted to cancelled", () => {
    expect(finishReasonToStatus("cancelled")).toBe("cancelled");
    expect(finishReasonToStatus("aborted")).toBe("cancelled");
  });
  it("maps timeout/provider_error/error to error", () => {
    expect(finishReasonToStatus("timeout")).toBe("error");
    expect(finishReasonToStatus("provider_error")).toBe("error");
    expect(finishReasonToStatus("error")).toBe("error");
  });
  it("maps stream_disconnected to interrupted", () => {
    expect(finishReasonToStatus("stream_disconnected")).toBe("interrupted");
  });
});

describe("message status accessors", () => {
  it("reads finish_reason from metadata", () => {
    expect(getMessageFinishReason(msg("length"))).toBe("length");
    expect(getMessageFinishReason(msg(undefined))).toBeNull();
    expect(getMessageStatus(msg("length"))).toBe("truncated");
    expect(getMessageStatus(msg(undefined))).toBeNull();
  });
  it("isPartialResult flags truncated/interrupted/cancelled", () => {
    expect(isPartialResult(msg("length"))).toBe(true);
    expect(isPartialResult(msg("stream_disconnected"))).toBe(true);
    expect(isPartialResult(msg("cancelled"))).toBe(true);
    expect(isPartialResult(msg("stop"))).toBe(false);
    expect(isPartialResult(msg(undefined))).toBe(false);
  });
});
