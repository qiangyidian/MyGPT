"use client";

import { useEffect, useState } from "react";

/**
 * Tracks browser online/offline state (surfing through subways / flaky Wi-Fi).
 * A false `online` value means fetch() calls will fail — the UI shows a
 * persistent "network disconnected" banner instead of scattered English
 * "Failed to fetch" toasts, and consumers can pause polling.
 */
export function useOnlineStatus(): boolean {
  const [online, setOnline] = useState<boolean>(() =>
    typeof navigator !== "undefined" ? navigator.onLine : true
  );

  useEffect(() => {
    const update = () => setOnline(navigator.onLine);
    update();
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    return () => {
      window.removeEventListener("online", update);
      window.removeEventListener("offline", update);
    };
  }, []);

  return online;
}
