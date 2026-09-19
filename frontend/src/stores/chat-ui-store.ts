"use client";

import { create } from "zustand";
import type { UserChatMode } from "@/lib/types";
import { isUserChatMode } from "@/lib/user-modes";

// localStorage schema versions for the persisted chat mode.
//
// v1 ("mygpt.chat.mode")    — original; default "auto".
// v2 ("mygpt.chat.mode.v2") — DEFAULT "deep_research" (caused demo leak; reverted).
// v3 ("mygpt.chat.mode.v3") — DEFAULT "auto"; picker had 6 modes
//                             (auto/search/deep_research/create/data_analysis/debate).
// v4 ("mygpt.chat.mode.v4") — picker now has FOUR modes:
//                             speed | expert | debate | hermes.
//                             Legacy v3 values are mapped onto these (see mapLegacyMode).
const MODE_KEY = "mygpt.chat.mode.v4";
const LEGACY_MODE_KEY_V3 = "mygpt.chat.mode.v3";
const EFFORT_KEY = "mygpt.chat.reasoningEffort";

/**
 * Default chat mode. 极速 (speed) = single-agent native answer, no multi-agent,
 * fastest first token — the safe everyday default.
 */
const DEFAULT_MODE: UserChatMode = "speed";

/**
 * Map a legacy (pre-v4) mode onto the current picker. deep_research becomes
 * 专家 (expert); debate is selectable again so it maps to itself; everything
 * else becomes 极速 (speed).
 */
function mapLegacyMode(v: string | null): UserChatMode {
  if (v === "debate") return "debate"; // 辩论现在是独立可选的模式
  if (v === "deep_research") return "expert"; // was multi-agent
  return "speed"; // auto/search/create/data_analysis/unknown → fast native
}

/**
 * Resolve the persisted mode, migrating a legacy v3 value exactly once. The v3
 * value is mapped onto the current picker values, written to v4, and the v3 key
 * cleared.
 */
function migrateMode(): UserChatMode {
  if (typeof window === "undefined") return DEFAULT_MODE;
  const legacy = window.localStorage.getItem(LEGACY_MODE_KEY_V3);
  const next = legacy ? mapLegacyMode(legacy) : DEFAULT_MODE;
  window.localStorage.setItem(MODE_KEY, next);
  window.localStorage.removeItem(LEGACY_MODE_KEY_V3);
  return next;
}

function readMode(): UserChatMode {
  if (typeof window === "undefined") return DEFAULT_MODE;
  const v = window.localStorage.getItem(MODE_KEY);
  if (v && isUserChatMode(v)) return v;
  return migrateMode();
}

interface ChatUiState {
  /** User-facing capability mode (persisted; default speed — see DEFAULT_MODE). */
  mode: UserChatMode;
  /** Reasoning-effort hint (B6); honored only by models that support it. */
  reasoningEffort: "low" | "medium" | "high";
  setMode: (m: UserChatMode) => void;
  setReasoningEffort: (v: "low" | "medium" | "high") => void;
}

function readEffort(): "low" | "medium" | "high" {
  if (typeof window === "undefined") return "medium";
  const v = window.localStorage.getItem(EFFORT_KEY);
  return v === "low" || v === "high" ? v : "medium";
}

export const useChatUiStore = create<ChatUiState>((set) => ({
  mode: readMode(),
  reasoningEffort: readEffort(),
  setMode: (m) => {
    if (typeof window !== "undefined") window.localStorage.setItem(MODE_KEY, m);
    set({ mode: m });
  },
  setReasoningEffort: (v) => {
    if (typeof window !== "undefined") window.localStorage.setItem(EFFORT_KEY, v);
    set({ reasoningEffort: v });
  },
}));

// Exposed for tests: force a clean migration re-run by clearing the v4 key.
