import { describe, expect, it } from "vitest";

import { filenameFromDisposition } from "../download";

describe("filenameFromDisposition", () => {
  it("无响应头时返回 null，由调用方兜底", () => {
    expect(filenameFromDisposition(null)).toBeNull();
    expect(filenameFromDisposition("")).toBeNull();
  });

  it("优先取 filename* 的 UTF-8 中文文件名", () => {
    expect(
      filenameFromDisposition(
        "attachment; filename=\"export-a1b2.md\"; filename*=UTF-8''%E9%A1%B9%E7%9B%AE%E8%AE%A8%E8%AE%BA-a1b2.md"
      )
    ).toBe("项目讨论-a1b2.md");
  });

  it("只有 ASCII fallback 时也能拿到名字", () => {
    expect(filenameFromDisposition('attachment; filename="chat-1.md"')).toBe("chat-1.md");
  });

  it("坏掉的百分号编码不打断下载，退化成原样字符串", () => {
    expect(filenameFromDisposition("attachment; filename*=UTF-8''%E4%B8-")).toBe("%E4%B8-");
  });

  it("带引号的 filename 会去掉引号", () => {
    expect(filenameFromDisposition('attachment; filename="a b.md"')).toBe("a b.md");
  });
});
