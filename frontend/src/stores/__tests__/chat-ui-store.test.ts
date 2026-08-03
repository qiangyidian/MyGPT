import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The chat-mode store is the front-line guard against the demo-data leak: its
 * DEFAULT must be "auto" (not deep_research), and a stale legacy
 * "deep_research" persisted by the old bad default must NOT survive into a new
 * session. These tests drive the v2→v3 migration with a controlled localStorage.
 */

type Store = Record<string, string>;

function makeStorage(initial: Store = {}) {
  const data = new Map<string, string>(Object.entries(initial));
  return {
    getItem: (k: string) => (data.has(k) ? (data.get(k) as string) : null),
    setItem: (k: string, v: string) => {
      data.set(k, String(v));
    },
    removeItem: (k: string) => {
      data.delete(k);
    },
    clear: () => data.clear(),
    key: (i: number) => Array.from(data.keys())[i] ?? null,
    get length() {
      return data.size;
    },
  } as unknown as Storage;
}

async function loadStore(initial: Store) {
  vi.resetModules();
  const storage = makeStorage(initial);
  // The store guards on `typeof window === "undefined"`; provide a window so it
  // actually reads localStorage (mirroring the browser).
  (globalThis as { window?: unknown }).window = { localStorage: storage };
  (globalThis as { localStorage?: Storage }).localStorage = storage;
  const mod = await import("@/stores/chat-ui-store");
  return { mod, storage };
}

beforeEach(() => {
  vi.resetModules();
});

afterEach(() => {
  // Strip the faked globals so they cannot leak into other test files.
  delete (globalThis as { window?: unknown }).window;
  delete (globalThis as { localStorage?: Storage }).localStorage;
});

describe("chat-ui-store mode default + migration", () => {
  it("defaults to auto with no prior localStorage", async () => {
    const { mod } = await loadStore({});
    expect(mod.useChatUiStore.getState().mode).toBe("auto");
  });

  it("does NOT inherit a legacy v2 deep_research (the bad old default)", async () => {
    const { mod, storage } = await loadStore({ "mygpt.chat.mode.v2": "deep_research" });
    // The dangerous inherited value must be discarded for the safe default.
    expect(mod.useChatUiStore.getState().mode).toBe("auto");
    // v3 written as auto, v2 cleared so the migration never re-runs.
    expect(storage.getItem("mygpt.chat.mode.v3")).toBe("auto");
    expect(storage.getItem("mygpt.chat.mode.v2")).toBeNull();
  });

  it("carries forward an explicit non-default v2 choice (e.g. search)", async () => {
    const { mod, storage } = await loadStore({ "mygpt.chat.mode.v2": "search" });
    expect(mod.useChatUiStore.getState().mode).toBe("search");
    expect(storage.getItem("mygpt.chat.mode.v3")).toBe("search");
    expect(storage.getItem("mygpt.chat.mode.v2")).toBeNull();
  });

  it("honors a genuine explicit deep_research stored under the new v3 schema", async () => {
    // A deep_research written to v3 was a deliberate post-fix choice — keep it.
    const { mod } = await loadStore({ "mygpt.chat.mode.v3": "deep_research" });
    expect(mod.useChatUiStore.getState().mode).toBe("deep_research");
  });

  it("setMode persists the choice under v3 (not v2)", async () => {
    const { mod, storage } = await loadStore({});
    mod.useChatUiStore.getState().setMode("deep_research");
    expect(mod.useChatUiStore.getState().mode).toBe("deep_research");
    expect(storage.getItem("mygpt.chat.mode.v3")).toBe("deep_research");
  });

  it("the persisted default mode and buildChatBody agree on auto", async () => {
    const { mod } = await loadStore({});
    const { buildChatBody } = await import("@/lib/chat-request");
    const mode = mod.useChatUiStore.getState().mode;
    // The request carries exactly the store mode; no hidden deep_research.
    expect(buildChatBody({ content: "你都能干什么", mode }).mode).toBe("auto");
  });
});
