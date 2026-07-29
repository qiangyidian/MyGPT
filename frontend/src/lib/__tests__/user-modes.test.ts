import { describe, it, expect } from "vitest";
import { USER_MODES, getModeMeta, isUserChatMode } from "@/lib/user-modes";

describe("user-modes", () => {
  it("exposes exactly the five user-facing modes", () => {
    const values = USER_MODES.map((m) => m.value);
    expect(values).toEqual([
      "auto",
      "search",
      "deep_research",
      "create",
      "data_analysis",
    ]);
  });

  it("never leaks internal runtime concepts to end users", () => {
    // The UI must not show Native / CrewAI / Runtime / agent_profile wording.
    const forbidden = ["Native", "CrewAI", "Runtime", "原生", "agent_profile", "execution_mode"];
    for (const m of USER_MODES) {
      const text = `${m.label} ${m.short} ${m.description}`;
      for (const bad of forbidden) {
        expect(text).not.toContain(bad);
      }
    }
  });

  it("every mode has a label, description, and icon", () => {
    for (const m of USER_MODES) {
      expect(m.label.length).toBeGreaterThan(0);
      expect(m.description.length).toBeGreaterThan(0);
      expect(m.icon).toBeDefined();
    }
  });

  it("getModeMeta falls back to auto for unknown input", () => {
    expect(getModeMeta("nonsense").value).toBe("auto");
    expect(getModeMeta(undefined).value).toBe("auto");
    expect(getModeMeta("deep_research").value).toBe("deep_research");
  });

  it("isUserChatMode narrows correctly", () => {
    expect(isUserChatMode("search")).toBe(true);
    expect(isUserChatMode("nope")).toBe(false);
    expect(isUserChatMode(undefined)).toBe(false);
  });
});
