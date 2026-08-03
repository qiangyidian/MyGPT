import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  onboardingPreferenceKey,
  isOnboardingSkipped,
  markOnboardingSkipped,
  clearOnboardingSkipped,
} from "@/lib/onboarding-preference";

// Minimal localStorage mock backing onto a plain object.
function createStorageMock() {
  const store = new Map<string, string>();
  const ls = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => {
      store.set(k, String(v));
    },
    removeItem: (k: string) => {
      store.delete(k);
    },
    clear: () => store.clear(),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    get length() {
      return store.size;
    },
  };
  return { ls, store };
}

describe("onboardingPreferenceKey", () => {
  it("is namespaced and per-user", () => {
    expect(onboardingPreferenceKey("u1")).toBe("aichat.onboarding.skipped.u1");
    expect(onboardingPreferenceKey("u2")).toBe("aichat.onboarding.skipped.u2");
    expect(onboardingPreferenceKey("u1")).not.toBe(onboardingPreferenceKey("u2"));
  });
});

describe("onboarding preference (with localStorage)", () => {
  let mock: ReturnType<typeof createStorageMock>;

  beforeEach(() => {
    mock = createStorageMock();
    vi.stubGlobal("localStorage", mock.ls);
    vi.stubGlobal("window", { localStorage: mock.ls });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("is not skipped by default", () => {
    expect(isOnboardingSkipped("u1")).toBe(false);
  });

  it("records and reads a skip", () => {
    markOnboardingSkipped("u1");
    expect(isOnboardingSkipped("u1")).toBe(true);
    expect(mock.store.get(onboardingPreferenceKey("u1"))).toBe("1");
  });

  it("clears the skip", () => {
    markOnboardingSkipped("u1");
    expect(isOnboardingSkipped("u1")).toBe(true);
    clearOnboardingSkipped("u1");
    expect(isOnboardingSkipped("u1")).toBe(false);
  });

  it("keeps users isolated", () => {
    markOnboardingSkipped("u1");
    expect(isOnboardingSkipped("u1")).toBe(true);
    expect(isOnboardingSkipped("u2")).toBe(false);
  });

  it("ignores empty user ids", () => {
    markOnboardingSkipped("");
    expect(isOnboardingSkipped("")).toBe(false);
  });
});

describe("onboarding preference (SSR / no storage)", () => {
  beforeEach(() => {
    // Simulate SSR: no window, no localStorage.
    vi.stubGlobal("window", undefined);
    vi.stubGlobal("localStorage", undefined);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("is never skipped without window", () => {
    expect(isOnboardingSkipped("u1")).toBe(false);
  });

  it("mark/clear are safe no-ops without window", () => {
    expect(() => {
      markOnboardingSkipped("u1");
      clearOnboardingSkipped("u1");
    }).not.toThrow();
  });
});
