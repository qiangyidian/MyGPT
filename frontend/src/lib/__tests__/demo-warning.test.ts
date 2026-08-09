import { describe, it, expect, beforeEach } from "vitest";
import { USER_MODES, getModeMeta, isSpecialMode } from "@/lib/user-modes";
import {
  useAgentRunStore,
  selectIsDemo,
} from "@/stores/agent-run-store";

describe("user-modes: label/value consistency + special modes", () => {
  it("getModeMeta(m).value === m for every mode (label never lies about value)", () => {
    for (const m of USER_MODES) {
      expect(getModeMeta(m.value).value).toBe(m.value);
    }
  });

  it("isSpecialMode flags exactly the multi-agent picker mode", () => {
    // 专家 (expert) is the only multi-agent picker mode → special (badge shown).
    expect(isSpecialMode("expert")).toBe(true);
    // 极速 (speed) and everything else are NOT special.
    expect(isSpecialMode("speed")).toBe(false);
    expect(isSpecialMode("auto")).toBe(false);
    expect(isSpecialMode("deep_research")).toBe(false);
    expect(isSpecialMode(undefined)).toBe(false);
  });
});

describe("agent-run-store: is_demo selector drives the demo warning", () => {
  beforeEach(() => {
    useAgentRunStore.getState().resetActive();
  });

  it("is false by default (normal mode shows no demo warning)", () => {
    expect(selectIsDemo(useAgentRunStore.getState())).toBe(false);
  });

  it("is true only when runtime_selection carries isDemo=true", () => {
    useAgentRunStore.getState().setRuntimeSelection({
      runId: "r1",
      requestedRuntime: "crewai",
      effectiveRuntime: "crewai",
      agentProfile: "deep_research",
      multiAgentRequested: true,
      multiAgentExecuted: true,
      fallbackReason: null,
      isDemo: true,
    });
    expect(selectIsDemo(useAgentRunStore.getState())).toBe(true);
  });

  it("is false for a real (non-demo) multi-agent run", () => {
    useAgentRunStore.getState().setRuntimeSelection({
      runId: "r2",
      requestedRuntime: "crewai",
      effectiveRuntime: "crewai",
      agentProfile: "deep_research",
      multiAgentRequested: true,
      multiAgentExecuted: true,
      fallbackReason: null,
      isDemo: false,
    });
    expect(selectIsDemo(useAgentRunStore.getState())).toBe(false);
  });
});
