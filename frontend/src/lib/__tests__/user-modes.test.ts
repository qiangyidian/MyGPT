import { describe, it, expect } from "vitest";
import { USER_MODES, getModeMeta, isUserChatMode, isSpecialMode } from "@/lib/user-modes";

describe("user-modes", () => {
  it("exposes exactly the two user-facing modes (speed | expert)", () => {
    const values = USER_MODES.map((m) => m.value);
    expect(values).toEqual(["speed", "expert"]);
  });

  it("expert mode is the multi-agent one; speed is not", () => {
    expect(isSpecialMode("expert")).toBe(true);
    expect(isSpecialMode("speed")).toBe(false);
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

  it("isUserChatMode accepts the two modes", () => {
    expect(isUserChatMode("speed")).toBe(true);
    expect(isUserChatMode("expert")).toBe(true);
    expect(isUserChatMode("nope")).toBe(false);
    expect(isUserChatMode(undefined)).toBe(false);
  });

  it("getModeMeta falls back to speed for unknown input", () => {
    expect(getModeMeta("nonsense").value).toBe("speed");
    expect(getModeMeta(undefined).value).toBe("speed");
    expect(getModeMeta("expert").value).toBe("expert");
  });
});
