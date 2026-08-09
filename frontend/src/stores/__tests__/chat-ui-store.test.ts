import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The chat-mode store persists the user's picker choice. The picker now exposes
 * exactly two modes — speed | expert — and migrates the legacy 6-mode v3 value
 * onto them: legacy multi-agent modes (deep_research, debate) → expert;
 * everything else → speed.
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
  (globalThis as { window?: unknown }).window = { localStorage: storage };
  (globalThis as { localStorage?: Storage }).localStorage = storage;
  const mod = await import("@/stores/chat-ui-store");
  return { mod, storage };
}

beforeEach(() => {
  vi.resetModules();
});

afterEach(() => {
  delete (globalThis as { window?: unknown }).window;
  delete (globalThis as { localStorage?: Storage }).localStorage;
});

describe("chat-ui-store mode default + v3→v4 migration", () => {
  it("defaults to speed with no prior localStorage", async () => {
    const { mod } = await loadStore({});
    expect(mod.useChatUiStore.getState().mode).toBe("speed");
  });

  it("migrates a legacy v3 deep_research to expert (multi-agent preserved)", async () => {
    const { mod, storage } = await loadStore({ "mygpt.chat.mode.v3": "deep_research" });
    expect(mod.useChatUiStore.getState().mode).toBe("expert");
    expect(storage.getItem("mygpt.chat.mode.v4")).toBe("expert");
    expect(storage.getItem("mygpt.chat.mode.v3")).toBeNull();
  });

  it("migrates a legacy v3 debate to expert too", async () => {
    const { mod } = await loadStore({ "mygpt.chat.mode.v3": "debate" });
    expect(mod.useChatUiStore.getState().mode).toBe("expert");
  });

  it("migrates a legacy v3 non-multi-agent mode (auto/search/create/data_analysis) to speed", async () => {
    for (const legacy of ["auto", "search", "create", "data_analysis"]) {
      const { mod } = await loadStore({ "mygpt.chat.mode.v3": legacy });
      expect(mod.useChatUiStore.getState().mode).toBe("speed");
    }
  });

  it("honors a genuine v4 choice (speed/expert) without migrating", async () => {
    const a = (await loadStore({ "mygpt.chat.mode.v4": "expert" })).mod;
    expect(a.useChatUiStore.getState().mode).toBe("expert");
    const b = (await loadStore({ "mygpt.chat.mode.v4": "speed" })).mod;
    expect(b.useChatUiStore.getState().mode).toBe("speed");
  });

  it("setMode persists the choice under v4", async () => {
    const { mod, storage } = await loadStore({});
    mod.useChatUiStore.getState().setMode("expert");
    expect(mod.useChatUiStore.getState().mode).toBe("expert");
    expect(storage.getItem("mygpt.chat.mode.v4")).toBe("expert");
  });

  it("the persisted default mode and buildChatBody agree on speed", async () => {
    const { mod } = await loadStore({});
    const { buildChatBody } = await import("@/lib/chat-request");
    const mode = mod.useChatUiStore.getState().mode;
    expect(buildChatBody({ content: "你都能干什么", mode }).mode).toBe("speed");
  });
});
