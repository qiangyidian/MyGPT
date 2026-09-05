"use client";

import { useEffect } from "react";

interface Shortcuts {
  /** Open a new chat (Cmd/Ctrl+Shift+O — ChatGPT-compatible). */
  onNewChat?: () => void;
  /** Focus the conversation search (Cmd/Ctrl+K). */
  onSearch?: () => void;
  /** Close the mobile sidebar (Escape). */
  onCloseSidebar?: () => void;
}

function isEditable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    target.isContentEditable
  );
}

/**
 * Global keyboard shortcuts (desktop power users):
 *   Cmd/Ctrl+K        — focus conversation search
 *   Cmd/Ctrl+Shift+O  — new chat
 *   Escape            — close the mobile sidebar drawer
 * IME-composition-safe: shortcuts are suppressed while composing text.
 */
export function useKeyboardShortcuts({
  onNewChat,
  onSearch,
  onCloseSidebar,
}: Shortcuts): void {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.isComposing) return;
      const mod = e.metaKey || e.ctrlKey;

      if (e.key === "Escape") {
        onCloseSidebar?.();
        return;
      }
      if (!mod) return;

      if (e.key.toLowerCase() === "k" && !e.shiftKey && !e.altKey) {
        // Let the browser's own focus-a-field shortcut win while typing.
        if (isEditable(e.target)) return;
        e.preventDefault();
        onSearch?.();
        return;
      }
      if (e.key.toLowerCase() === "o" && e.shiftKey && !e.altKey) {
        e.preventDefault();
        onNewChat?.();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onNewChat, onSearch, onCloseSidebar]);
}
