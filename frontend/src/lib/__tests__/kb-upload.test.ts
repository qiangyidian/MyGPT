// 知识库上传的「接受什么文件」由服务端说了算（条目 19）；这里守住客户端那一半。
import { describe, expect, it } from "vitest";

import {
  FALLBACK_UPLOAD_EXTENSIONS,
  describeKbUpload,
  effectiveExtensions,
  isUploadableToKb,
  kbAcceptAttribute,
  kbUploadRejectionMessage,
} from "@/lib/kb-upload";

const CAPS = {
  allowed_extensions: [".pdf", ".docx", ".doc", ".pptx", ".md", ".markdown", ".epub"],
  max_upload_mb: 20,
};

describe("effectiveExtensions", () => {
  it("uses the server answer verbatim", () => {
    expect(effectiveExtensions(CAPS)).toEqual(CAPS.allowed_extensions);
  });

  it("falls back while the request is in flight or when it returns nothing", () => {
    expect(effectiveExtensions(undefined)).toEqual(FALLBACK_UPLOAD_EXTENSIONS);
    expect(effectiveExtensions({ allowed_extensions: [], max_upload_mb: 20 })).toEqual(
      FALLBACK_UPLOAD_EXTENSIONS,
    );
  });

  it("keeps only types the parser registry can actually read", () => {
    // 兜底清单不能包含服务端会拒的类型：否则「选得到、传不上」。
    expect(FALLBACK_UPLOAD_EXTENSIONS).toContain(".pdf");
    expect(FALLBACK_UPLOAD_EXTENSIONS).not.toContain(".exe");
    expect(FALLBACK_UPLOAD_EXTENSIONS).not.toContain(".png");
  });
});

describe("kbAcceptAttribute", () => {
  it("is the comma-joined dotted list the file picker wants", () => {
    expect(kbAcceptAttribute(CAPS)).toBe(CAPS.allowed_extensions.join(","));
  });
});

describe("isUploadableToKb", () => {
  it("matches on a case-insensitive suffix", () => {
    expect(isUploadableToKb({ name: "季度.PDF" }, CAPS)).toBe(true);
    expect(isUploadableToKb({ name: "README.markdown" }, CAPS)).toBe(true);
    expect(isUploadableToKb({ name: "tool.exe" }, CAPS)).toBe(false);
    expect(isUploadableToKb({ name: "no_extension" }, CAPS)).toBe(false);
  });
});

describe("describeKbUpload", () => {
  it("groups extensions into readable names and states the size cap", () => {
    const text = describeKbUpload(CAPS);
    expect(text).toContain("PDF");
    expect(text).toContain("Word");
    expect(text).toContain("PPT");
    expect(text).toContain("Markdown");
    expect(text).toContain("EPUB");
    expect(text).toContain("20MB");
    // 同类只出现一次（.docx/.doc 都是 Word）
    expect(text.match(/Word/g)).toHaveLength(1);
  });

  it("still reads sensibly before the server answers", () => {
    expect(describeKbUpload(undefined)).toContain("PDF");
  });
});

describe("kbUploadRejectionMessage", () => {
  it("stays silent for an allowed file", () => {
    expect(kbUploadRejectionMessage({ name: "a.pdf" }, CAPS)).toBeNull();
  });

  it("names the offending extension for a rejected one", () => {
    const msg = kbUploadRejectionMessage({ name: "a.zip" }, CAPS);
    expect(msg).toContain(".zip");
    expect(msg).toContain("支持");
  });
});
