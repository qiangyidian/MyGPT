import { describe, it, expect } from "vitest";
import { sanitizeSourceMarkers, citedCitations } from "@/lib/citations";
import type { Citation } from "@/lib/types";

const cite = (id: string, name: string): Citation => ({
  document_id: id,
  document_name: name,
  chunk_id: null,
  chunk_index: 0,
  snippet: "",
  score: 0.5,
});

describe("sanitizeSourceMarkers (citation integrity)", () => {
  it("strips every marker when there are no citations", () => {
    expect(sanitizeSourceMarkers("blah [source 1]", 0)).toBe("blah");
  });

  it("keeps backed markers, strips unbacked ones", () => {
    const out = sanitizeSourceMarkers("a [source 1] b [source 2]", 1);
    expect(out).toContain("[source 1]");
    expect(out).not.toContain("[source 2]");
  });

  it("leaves text unchanged when all markers are backed", () => {
    const text = "x [source 1] y [source 2]";
    expect(sanitizeSourceMarkers(text, 2)).toBe(text);
  });

  it("leaves text unchanged when there are no markers", () => {
    expect(sanitizeSourceMarkers("plain answer", 0)).toBe("plain answer");
  });

  it("handles Chinese 来源 and case-insensitive Source", () => {
    expect(sanitizeSourceMarkers("数据见[来源 3]", 0)).toBe("数据见");
    expect(sanitizeSourceMarkers("see [Source 1]", 1)).toBe("see [Source 1]");
  });

  it("handles no-space and colon marker variants", () => {
    const out = sanitizeSourceMarkers("see [source1] and [source: 2]", 1);
    expect(out).toContain("[source1]"); // backed (n=1) — kept
    expect(out).not.toContain("[source: 2]"); // unbacked (n=2) — stripped
    expect(sanitizeSourceMarkers("fullwidth [来源：1]", 1)).toBe("fullwidth [来源：1]");
  });
});

describe("citedCitations (chip grid = actually-cited candidates only)", () => {
  const cands = [cite("d1", "a"), cite("d2", "b"), cite("d3", "c")];

  it("keeps only candidates the text marks as [source N], in original positions", () => {
    const out = citedCitations(" proved [source 2] plus [source 1]", cands);
    expect(out).toEqual([cands[0], cands[1]]);
  });

  it("out-of-range markers never fabricate a chip", () => {
    expect(citedCitations("see [source 9]", cands)).toEqual([]);
  });

  it("no markers → no chips (candidates are not citations)", () => {
    expect(citedCitations("plain answer", cands)).toEqual([]);
  });

  it("handles Chinese 来源 and colon/no-space variants", () => {
    expect(citedCitations("数据见[来源：3]", cands)).toEqual([cands[2]]);
    expect(citedCitations("see [source2]", cands)).toEqual([cands[1]]);
  });

  it("empty text or empty candidate list → nothing", () => {
    expect(citedCitations("", cands)).toEqual([]);
    expect(citedCitations("x [source 1]", [])).toEqual([]);
    expect(citedCitations(null, cands)).toEqual([]);
  });
});
