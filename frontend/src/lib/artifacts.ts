/**
 * Pure helpers for tenant-scoped artifacts (Task 12).
 *
 * Artifacts are referenced by an opaque id; the client never sees a storage
 * path. Bytes are fetched through the authenticated `GET /api/artifacts/{id}`
 * endpoint. Messages reference artifacts via `artifact:<id>` handles (and, for
 * typed multimodal content, a `MessagePart` of type `"artifact"`).
 */

import type { ArtifactMeta } from "./types";

/** Re-export for convenience so callers can import guards + types together. */
export type { ArtifactMeta } from "./types";

const UUID_RE =
  /[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/;
const ARTIFACT_HANDLE_RE = /\bartifact:([0-9a-fA-F-]{36})\b/g;

/** Type guard for the artifact summary shape returned by `/api/artifacts`. */
export function isArtifactMeta(v: unknown): v is ArtifactMeta {
  if (!v || typeof v !== "object") return false;
  const o = v as Record<string, unknown>;
  return (
    typeof o.id === "string" &&
    typeof o.media_type === "string" &&
    typeof o.size === "number" &&
    Number.isFinite(o.size) &&
    typeof o.source === "string"
  );
}

/**
 * Parse a single `artifact:<uuid>` handle out of `text`. Returns the artifact
 * id, or null when no handle is present.
 */
export function parseArtifactHandle(text: string): string | null {
  const m = text.match(ARTIFACT_HANDLE_RE);
  if (!m || m.length === 0) return null;
  const single = m[0].slice("artifact:".length);
  return UUID_RE.test(single) ? single : null;
}

/**
 * Extract every distinct `artifact:<uuid>` handle in `text`, in first-seen
 * order. Use this to render artifact cards inline within a message bubble.
 */
export function findArtifactHandles(text: string): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const match of text.matchAll(ARTIFACT_HANDLE_RE)) {
    const id = match[1];
    if (UUID_RE.test(id) && !seen.has(id)) {
      seen.add(id);
      out.push(id);
    }
  }
  return out;
}

/** Human-readable byte size (B / KB / MB / GB). */
export function formatArtifactSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(
    units.length - 1,
    Math.floor(Math.log(bytes) / Math.log(1024)),
  );
  const value = bytes / Math.pow(1024, i);
  // Bytes are integer; larger units get one decimal place.
  return i === 0 ? `${Math.round(value)} ${units[i]}` : `${value.toFixed(1)} ${units[i]}`;
}
