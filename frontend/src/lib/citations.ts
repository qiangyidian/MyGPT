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
