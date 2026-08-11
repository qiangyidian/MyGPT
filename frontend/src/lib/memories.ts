/**
 * User-level semantic memory helpers (Task 12).
 *
 * User memory is OPT-IN: a proposed memory is created inactive and only folds
 * into the effective prompt after the user explicitly activates it. These
 * helpers encode that default and the "active right now" guard (active AND
 * not expired).
 */

import type { UserMemory, UserMemoryProposeInput } from "./types";

/**
 * The default propose body. `active` is always false — activation is a
 * separate, explicit user action (`POST /api/memories/{id}/activate`).
 */
export function DEFAULT_USER_MEMORY_PROPOSE(
  content: string,
): UserMemoryProposeInput {
  return {
    content,
    memory_type: "fact",
    confidence: 0.5,
    active: false,
  };
}

/**
 * True when a memory is ACTIVE and not expired (i.e. it currently folds into
 * the effective prompt). An expired-but-still-active row is treated as
 * inactive — the backend suppresses it from retrieval.
 */
export function userMemoryIsActive(memory: UserMemory): boolean {
  if (!memory.active) return false;
  if (memory.expires_at) {
    const expires = Date.parse(memory.expires_at);
    if (Number.isFinite(expires) && expires <= Date.now()) return false;
  }
  return true;
}
