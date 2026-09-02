"use client";

// Access token lives in memory (reload-safe via optional sessionStorage) so it is
// never in a long-lived store. Refresh token is an httpOnly cookie set by the backend.

const KEY = "aichat.access_token";
let inMemory: string | null = null;

export function getAccessToken(): string | null {
  if (inMemory) return inMemory;
  if (typeof window !== "undefined") {
    inMemory = sessionStorage.getItem(KEY);
  }
  return inMemory;
}

export function setAccessToken(token: string | null): void {
  inMemory = token;
  if (typeof window === "undefined") return;
  if (token) sessionStorage.setItem(KEY, token);
  else sessionStorage.removeItem(KEY);
}

