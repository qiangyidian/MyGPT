import { describe, it, expect } from "vitest";
import { sanitizeSourceMarkers } from "@/lib/citations";

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
