// 会话级系统提示词的编辑逻辑（条目 31）。
//
// 「留空 = 恢复平台默认」是这套交互里唯一的语义陷阱：发错一个字段就会把
// 会话的自定义设定悄悄清掉，或者反过来让用户以为清空了却还留着。
import { describe, expect, it } from "vitest";

import {
  SYSTEM_PROMPT_MAX_CHARS,
  canRestoreDefaultPrompt,
  countPromptChars,
  systemPromptError,
  systemPromptPatch,
} from "@/lib/system-prompt";

describe("countPromptChars", () => {
  it("counts Chinese by character, not by bytes", () => {
    expect(countPromptChars("用简体中文回答")).toBe(7);
  });

  it("counts an astral character as one, matching what the user sees", () => {
    // String.length 会把 😀 算成 2 —— 那会让计数器比 maxLength 早一格。
    expect("😀".length).toBe(2);
    expect(countPromptChars("😀")).toBe(1);
    expect(countPromptChars("a😀b")).toBe(3);
  });

  it("counts blank input as zero so the empty state is detectable", () => {
    expect(countPromptChars("")).toBe(0);
    expect(countPromptChars("   ")).toBe(3);
  });
});

describe("systemPromptError", () => {
  it("accepts up to the client budget", () => {
    expect(systemPromptError("a".repeat(SYSTEM_PROMPT_MAX_CHARS))).toBeNull();
  });

  it("quotes both the limit and the current count once over", () => {
    const msg = systemPromptError("a".repeat(SYSTEM_PROMPT_MAX_CHARS + 1));
    expect(msg).toContain(String(SYSTEM_PROMPT_MAX_CHARS));
    expect(msg).toContain(String(SYSTEM_PROMPT_MAX_CHARS + 1));
  });
});

describe("systemPromptPatch", () => {
  it("sends nothing when nothing changed", () => {
    expect(systemPromptPatch(null, "")).toBeNull();
    expect(systemPromptPatch("  ", "")).toBeNull();
    expect(systemPromptPatch("简洁回答", "简洁回答")).toBeNull();
    expect(systemPromptPatch("简洁回答", "  简洁回答  ")).toBeNull();
  });

  it("sends the trimmed text when there is one", () => {
    expect(systemPromptPatch(null, "  用中文回答  ")).toEqual({
      system_prompt: "用中文回答",
    });
    expect(systemPromptPatch("旧设定", "新设定")).toEqual({
      system_prompt: "新设定",
    });
  });

  it("clears the column back to the platform default, not to an empty string", () => {
    // 后端 _build_system_prompt 把 NULL 当作「用默认」，存 "" 会被当成一份
    // 真实为空的角色设定。
    expect(systemPromptPatch("旧设定", "   ")).toEqual({ system_prompt: null });
  });
});

describe("canRestoreDefaultPrompt", () => {
  it("is a no-op when the conversation already uses the default", () => {
    expect(canRestoreDefaultPrompt(null, "")).toBe(false);
    expect(canRestoreDefaultPrompt("   ", "")).toBe(false);
  });

  it("is offered whenever there is something on screen or a stored override", () => {
    expect(canRestoreDefaultPrompt(null, "临时设定")).toBe(true);
    expect(canRestoreDefaultPrompt("自定义", "")).toBe(true);
  });
});
