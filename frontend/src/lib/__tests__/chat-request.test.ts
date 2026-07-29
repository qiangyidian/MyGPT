import { describe, it, expect } from "vitest";
import { buildChatBody } from "@/lib/chat-request";

describe("buildChatBody (mode → request mapping)", () => {
  it("sends the user-facing mode, not internal runtime enums", () => {
    const body = buildChatBody({ content: "hi", mode: "search", attachmentIds: ["a1"] });
    expect(body.mode).toBe("search");
    expect(body.attachment_ids).toEqual(["a1"]);
    // Internal fields must NOT be carried on the wire.
    expect(body.execution_mode).toBeUndefined();
    expect(body.agent_profile).toBeUndefined();
    expect(body.enable_tools).toBeUndefined();
  });

  it("defaults mode to auto and attachments to empty", () => {
    const body = buildChatBody({ content: "hi" });
    expect(body.mode).toBe("auto");
    expect(body.attachment_ids).toEqual([]);
    expect(body.regenerate).toBe(false);
  });

  it("carries conversation/model/kb ids through", () => {
    const body = buildChatBody({
      content: "hi",
      conversationId: "c1",
      modelId: "m1",
      knowledgeBaseId: "k1",
      regenerate: true,
    });
    expect(body.conversation_id).toBe("c1");
    expect(body.model_id).toBe("m1");
    expect(body.knowledge_base_id).toBe("k1");
    expect(body.regenerate).toBe(true);
  });
});
