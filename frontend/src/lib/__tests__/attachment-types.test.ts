// Client-side attachment type allow-list tests (mirrors backend ATTACHMENT_ALLOWED_EXT).
import { describe, expect, it } from "vitest";

import {
  ATTACHMENT_ACCEPT,
  attachmentRejectionMessage,
  isAllowedAttachment,
} from "@/lib/attachment-types";

describe("isAllowedAttachment", () => {
  it.each([
    "report.pdf",
    "文档.docx",
    "data.xlsx",
    "notes.md",
    "photo.PNG",
    "song.MP3",
    "slides.pptx",
    "book.epub",
  ])("accepts %s", (name) => {
    expect(isAllowedAttachment({ name })).toBe(true);
  });

  it.each([
    "app.exe",
    "archive.zip",
    "video.mp4",
    "script.sh",
    "noext",
    "font.woff2",
  ])("rejects %s", (name) => {
    expect(isAllowedAttachment({ name })).toBe(false);
  });

  it("is case-insensitive on extension", () => {
    expect(isAllowedAttachment({ name: "IMG.JPEG" })).toBe(true);
    expect(isAllowedAttachment({ name: "BAD.EXE" })).toBe(false);
  });
});

describe("attachmentRejectionMessage", () => {
  it("returns null when all files are allowed", () => {
    expect(attachmentRejectionMessage([{ name: "a.pdf" }, { name: "b.png" }])).toBeNull();
  });

  it("names the offending file when mixed", () => {
    const msg = attachmentRejectionMessage([{ name: "ok.pdf" }, { name: "bad.exe" }]);
    expect(msg).toContain("bad.exe");
    expect(msg).not.toContain("ok.pdf");
  });

  it("caps the listed offenders at 3 with a total count", () => {
    const msg = attachmentRejectionMessage([
      { name: "1.exe" }, { name: "2.zip" }, { name: "3.rar" }, { name: "7z.exe" },
    ]);
    expect(msg).toContain("1.exe");
    expect(msg).toContain("4 个文件");
  });
});

describe("ATTACHMENT_ACCEPT", () => {
  it("is a comma-separated extension list usable as an accept attribute", () => {
    expect(ATTACHMENT_ACCEPT.startsWith(".pdf")).toBe(true);
    expect(ATTACHMENT_ACCEPT).toContain(".docx");
    expect(ATTACHMENT_ACCEPT).not.toContain(".exe");
  });
});
