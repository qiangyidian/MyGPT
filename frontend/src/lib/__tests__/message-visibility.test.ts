import { describe, expect, it } from "vitest";

import { getVisibleMessages } from "@/lib/message-visibility";
import type { Message } from "@/lib/types";

const message = (id: string, role: Message["role"], content: string): Message => ({
  id,
  conversation_id: "conversation-1",
  role,
  content,
  metadata: {},
  model_name: null,
  created_at: "2026-08-09T00:00:00.000Z",
});

describe("getVisibleMessages", () => {
  it("hides every empty assistant placeholder while a separate stream is rendered", () => {
    const messages = [
      message("assistant-placeholder", "assistant", ""),
      message("user-message", "user", "2026 年有哪些新开源大模型"),
    ];

    expect(getVisibleMessages(messages, true)).toEqual([messages[1]]);
  });

  it("keeps empty assistant messages once streaming has ended", () => {
    const messages = [message("assistant-empty", "assistant", "")];

    expect(getVisibleMessages(messages, false)).toEqual(messages);
  });
});
