"use client";

import { create } from "zustand";
import type { UserChatMode } from "@/lib/types";
import { isUserChatMode } from "@/lib/user-modes";

// localStorage schema versions for the persisted chat mode.
//
// v1 ("mygpt.chat.mode")   — original; default was "auto".
// v2 ("mygpt.chat.mode.v2") — DEFAULT was changed to "deep_research" so every
//                            new conversation ran the multi-agent crew. That
//                            default was the root cause of demo data leaking
//                            into normal chat, so it is being reverted.
// v3 ("mygpt.chat.mode.v3") — DEFAULT is "auto" again. See migrateMode() for
//                            why we cannot simply re-read v2: a stored
//                            "deep_research" there is ambiguous (real choice
//                            vs. the inherited bad default), so on the v2→v3
//                            migration we DROP a stored deep_research and keep
//                            any other explicit choice.
const MODE_KEY = "mygpt.chat.mode.v3";
const LEGACY_MODE_KEY_V2 = "mygpt.chat.mode.v2";
const ADV_MODEL_KEY = "mygpt.chat.advancedModel";

/**
 * Default chat mode. ``auto`` lets the backend IntentRouter pick native chat
 * for ordinary questions and only escalate to multi-agent/debate when the user
 * explicitly asks. This is the safe default: a brand-new user typing "你好" or
 * "你都能干什么" gets a real answer, never the canned demo crew output.
 */
const DEFAULT_MODE: UserChatMode = "auto";

/**
 * Resolve the persisted mode, migrating a legacy v2 value exactly once.
 *
 * Why migrate instead of just bumping the key: we want to PRESERVE a user's
 * explicit non-default choice (e.g. they deliberately picked "search" or
 * "create"), but a stored "deep_research" under v2 is ambiguous — it may be a
 * genuine choice OR merely the old bad default that was written automatically.
 * We cannot tell them apart, and the inherited deep_research is exactly the bug
 * we are fixing (it silently routed every new chat through the demo path). So:
 *   • any v2 value that is NOT deep_research → carry forward (explicit choice);
 *   • a v2 deep_research (or no v2 at all) → fall back to the new default auto.
 * The migrated value is written to v3 and the v2 key is cleared so this runs
 * only once.
 */
function migrateMode(): UserChatMode {
  if (typeof window === "undefined") return DEFAULT_MODE;
  const current = window.localStorage.getItem(MODE_KEY);
  if (isUserChatMode(current)) return current;

  const legacy = window.localStorage.getItem(LEGACY_MODE_KEY_V2);
  if (legacy && legacy !== "deep_research" && isUserChatMode(legacy)) {
    // An explicit, non-dangerous choice under the old schema — keep it.
    window.localStorage.setItem(MODE_KEY, legacy);
  } else {
    // Either no legacy value, or the legacy value was the dangerous inherited
    // deep_research default — adopt the safe new default.
    window.localStorage.setItem(MODE_KEY, DEFAULT_MODE);
  }
  window.localStorage.removeItem(LEGACY_MODE_KEY_V2);
  return isUserChatMode(legacy) && legacy !== "deep_research" ? legacy : DEFAULT_MODE;
}

function readMode(): UserChatMode {
  if (typeof window === "undefined") return DEFAULT_MODE;
  const v = window.localStorage.getItem(MODE_KEY);
  return isUserChatMode(v) ? v : migrateMode();
}

function readAdv(): boolean {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(ADV_MODEL_KEY) === "1";
}

interface ChatUiState {
  /** User-facing capability mode (persisted; default auto — see DEFAULT_MODE). */
  mode: UserChatMode;
  /** Show the model selector in the composer (admin / advanced users). */
  showAdvancedModel: boolean;
  setMode: (m: UserChatMode) => void;
  setShowAdvancedModel: (b: boolean) => void;
}

export const useChatUiStore = create<ChatUiState>((set) => ({
  mode: readMode(),
  showAdvancedModel: readAdv(),
  setMode: (m) => {
    if (typeof window !== "undefined") window.localStorage.setItem(MODE_KEY, m);
    set({ mode: m });
  },
  setShowAdvancedModel: (b) => {
    if (typeof window !== "undefined") window.localStorage.setItem(ADV_MODEL_KEY, b ? "1" : "0");
    set({ showAdvancedModel: b });
  },
}));

// Exposed for tests: force a clean migration re-run by clearing the v3 key.
export const __modeKeys = { MODE_KEY, LEGACY_MODE_KEY_V2 };
