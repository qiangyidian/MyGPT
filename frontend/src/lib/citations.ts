/**
 * Citation integrity helper — mirror of the backend
 * app/rag/citations.py:sanitize_unbacked_source_markers.
 *
 * The model (and the deterministic demo writer) may emit ``[source N]`` markers
 * that have no real backing citation. The frontend renders citations from the
 * STRUCTURED citation metadata (the <Citations/> chips), not from text regex —
 * so an unbacked in-text marker would just read as a dangling "[source 5]". This
 * strips any marker whose number has no matching citation before rendering, so
 * both the live stream and persisted messages show honest text.
 */
export function sanitizeSourceMarkers(
  text: string | null | undefined,
  citationCount: number,
): string {
  if (!text) return text ?? "";
  // Fresh regex per call (a module-level /g literal would be fine with .replace,
  // but constructing here avoids any shared-state ambiguity across calls).
  // Tolerant of internal whitespace and colon separators, and the no-space
  // variant ("[source1]"), to match what a model/demo can realistically emit.
  const marker = /\[\s*(?:source|来源)[\s:：]*(\d+)\s*\]/gi;
  let changed = false;
  const out = text.replace(marker, (whole, n) => {
    const num = Number(n);
    if (citationCount > 0 && num >= 1 && num <= citationCount) {
      return whole; // backed by a real citation — keep verbatim
    }
    changed = true;
    return ""; // unbacked — strip the fabricated marker
  });
  if (!changed) return text;
  // Tidy the whitespace a removed inline marker leaves behind.
  return out
    .replace(/[ \t]{2,}/g, " ")
    .replace(/\s+([，。、；;：:！？!?,.])/g, "$1")
    .trim();
}

/**
 * Real-citations filter: the backend emits ALL retrieval candidates as the
 * ``citations`` event / metadata BEFORE the first token — that list is "what
 * was retrieved", not "what was cited". The in-text ``[source N]`` markers are
 * the only honest record of which sources the answer actually used, so the
 * chip grid keeps just the cited candidates. Chips keep their ORIGINAL list
 * positions so an in-text ``[source 3]`` still opens source 3.
 * Empty result (answer cited nothing) means the caller renders no chips.
 */
import type { Citation } from "@/lib/types";

export function citedCitations(
  text: string | null | undefined,
  citations: readonly Citation[] | null | undefined,
): Citation[] {
  if (!Array.isArray(citations) || citations.length === 0) return [];
  const cited = new Set<number>();
  const marker = /\[\s*(?:source|来源)[\s:：]*(\d+)\s*\]/gi;
  for (const m of (text ?? "").matchAll(marker)) {
    const n = Number(m[1]);
    if (n >= 1 && n <= citations.length) cited.add(n);
  }
  return citations.filter((_, i) => cited.has(i + 1));
}
