"use client";

import { create } from "zustand";
import type { UserChatMode } from "@/lib/types";
import { isUserChatMode } from "@/lib/user-modes";

const MODE_KEY = "mygpt.chat.mode";
const ADV_MODEL_KEY = "mygpt.chat.advancedModel";

function readMode(): UserChatMode {
  if (typeof window === "undefined") return "auto";
  const v = window.localStorage.getItem(MODE_KEY);
  return isUserChatMode(v) ? v : "auto";
}

function readAdv(): boolean {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(ADV_MODEL_KEY) === "1";
}

interface ChatUiState {
  /** User-facing capability mode (persisted; default auto for new conversations). */
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
