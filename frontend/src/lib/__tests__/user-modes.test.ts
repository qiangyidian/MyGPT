import { describe, it, expect } from "vitest";
import { USER_MODES, getModeMeta, isUserChatMode, isSpecialMode } from "@/lib/user-modes";

describe("user-modes", () => {
  it("exposes exactly the four user-facing modes (speed | expert | debate | hermes)", () => {
    const values = USER_MODES.map((m) => m.value);
    expect(values).toEqual(["speed", "expert", "debate", "hermes"]);
  });

  it("expert mode is the multi-agent one; speed/hermes are not", () => {
    expect(isSpecialMode("expert")).toBe(true);
    expect(isSpecialMode("speed")).toBe(false);
    expect(isSpecialMode("hermes")).toBe(false);
  });

  it("each mode has friendly copy and hides internal runtime names", () => {
    for (const m of USER_MODES) {
      expect(m.label.length).toBeGreaterThan(0);
      expect(m.description.length).toBeGreaterThan(0);
      expect(m.icon).toBeDefined();
      // Internal runtime enums must NOT leak into user-facing copy.
      expect(`${m.label} ${m.short} ${m.description}`).not.toMatch(
        /CrewAI|NativeRuntime|execution_mode|agent_profile/,
      );
    }
  });

  it("isUserChatMode accepts the selectable modes", () => {
    expect(isUserChatMode("speed")).toBe(true);
    expect(isUserChatMode("expert")).toBe(true);
    expect(isUserChatMode("debate")).toBe(true);
    expect(isUserChatMode("nope")).toBe(false);
    expect(isUserChatMode(undefined)).toBe(false);
  });

  it("getModeMeta falls back to speed for unknown input", () => {
    expect(getModeMeta("nonsense").value).toBe("speed");
    expect(getModeMeta(undefined).value).toBe("speed");
    expect(getModeMeta("expert").value).toBe("expert");
  });
});

describe("辩论模式", () => {
  it("出现在模式选择器里", () => {
    const debate = USER_MODES.find((m) => m.value === "debate");
    expect(debate).toBeDefined();
    expect(debate?.label).toContain("辩论");
  });

  it("被标记为特殊模式（composer 显示徽章）", () => {
    expect(isSpecialMode("debate")).toBe(true);
  });

  it("与 expert 一样是多 Agent 模式", () => {
    expect(isSpecialMode("expert")).toBe(true);
    expect(isSpecialMode("speed")).toBe(false);
  });

  it("极速模式不是特殊模式", () => {
    expect(isSpecialMode("hermes")).toBe(false);
  });
});
