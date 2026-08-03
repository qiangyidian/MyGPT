/**
 * Per-user "skip onboarding" preference, persisted in localStorage.
 *
 * Solves the /onboarding ↔ / bounce loop: an admin with no real chat model is
 * sent to /onboarding by AppShell, but "稍后配置（用 Mock 体验）" must be able to
 * escape to the home page without AppShell immediately bouncing them back.
 *
 * The flag is keyed by user id so different accounts sharing a browser don't
 * affect each other. Every accessor is SSR-safe (no `window` access on the
 * server) and tolerates localStorage being unavailable (private mode).
 */

const PREFIX = "aichat.onboarding.skipped.";
const SKIPPED_VALUE = "1";

/** Build the storage key for a given user (exported for unit testing). */
export function onboardingPreferenceKey(userId: string): string {
  return `${PREFIX}${userId}`;
}

function getStorage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    // Touching localStorage can throw in private mode / disabled storage.
    const ls = window.localStorage;
    if (!ls) return null;
    return ls;
  } catch {
    return null;
  }
}

/** True when `userId` has previously dismissed the onboarding wizard. */
export function isOnboardingSkipped(userId: string): boolean {
  if (!userId) return false;
  const storage = getStorage();
  if (!storage) return false;
  try {
    return storage.getItem(onboardingPreferenceKey(userId)) === SKIPPED_VALUE;
  } catch {
    return false;
  }
}

/** Record that `userId` has dismissed the onboarding wizard. */
export function markOnboardingSkipped(userId: string): void {
  if (!userId) return;
  const storage = getStorage();
  if (!storage) return;
  try {
    storage.setItem(onboardingPreferenceKey(userId), SKIPPED_VALUE);
  } catch {
    // Ignore write failures (quota / private mode).
  }
}

/** Clear the skip flag (e.g. once a real model has been configured). */
export function clearOnboardingSkipped(userId: string): void {
  if (!userId) return;
  const storage = getStorage();
  if (!storage) return;
  try {
    storage.removeItem(onboardingPreferenceKey(userId));
  } catch {
    // Ignore.
  }
}
