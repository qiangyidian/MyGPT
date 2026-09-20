// 摄取队列的用户可见文案（条目 18 的前端一半）。队列会自己重试，
// 界面上必须说清「还会再试」和「已经放弃、要人工处理」的差别。
import { describe, expect, it } from "vitest";

import { describeIngestQueue } from "@/lib/kb-ingest";
import type { DocFile } from "@/lib/types";

const NOW = Date.parse("2026-09-20T12:00:00.000Z");

function doc(overrides: Partial<DocFile> = {}): DocFile {
  return {
    status: "pending",
    ingest_attempts: 0,
    ingest_next_retry_at: null,
    ...overrides,
  } as DocFile;
}

function iso(secondsFromNow: number): string {
  return new Date(NOW + secondsFromNow * 1000).toISOString();
}

describe("describeIngestQueue", () => {
  it("stays quiet on a plain fresh upload", () => {
    expect(describeIngestQueue(doc(), NOW)).toBeNull();
    expect(describeIngestQueue(doc({ status: "parsing" }), NOW)).toBeNull();
    expect(
      describeIngestQueue(doc({ status: "indexed", ingest_attempts: 1 }), NOW),
    ).toBeNull();
  });

  it("announces a scheduled retry with a human wait", () => {
    expect(
      describeIngestQueue(
        doc({
          status: "pending",
          ingest_attempts: 1,
          ingest_next_retry_at: iso(45),
        }),
        NOW,
      ),
    ).toBe("第 1 次失败，45 秒后重试");
    expect(
      describeIngestQueue(
        doc({ ingest_attempts: 2, ingest_next_retry_at: iso(600) }),
        NOW,
      ),
    ).toBe("第 2 次失败，10 分钟后重试");
    expect(
      describeIngestQueue(
        doc({ ingest_attempts: 3, ingest_next_retry_at: iso(7200) }),
        NOW,
      ),
    ).toBe("第 3 次失败，2 小时后重试");
  });

  it("says 即将重试 once the backoff window has passed", () => {
    expect(
      describeIngestQueue(
        doc({ ingest_attempts: 3, ingest_next_retry_at: iso(-5) }),
        NOW,
      ),
    ).toBe("第 3 次失败，即将重试");
  });

  it("counts from the second attempt while a retry is running", () => {
    expect(
      describeIngestQueue(
        doc({ status: "embedding", ingest_attempts: 2 }),
        NOW,
      ),
    ).toBe("第 2 次尝试中");
    // 第一次尝试不需要解释自己
    expect(
      describeIngestQueue(
        doc({ status: "embedding", ingest_attempts: 1 }),
        NOW,
      ),
    ).toBeNull();
  });

  it("marks a retired document as needing manual action", () => {
    expect(
      describeIngestQueue(doc({ status: "failed", ingest_attempts: 4 }), NOW),
    ).toBe("已尝试 4 次，不再自动重试");
  });

  it("ignores an unparseable retry timestamp", () => {
    expect(
      describeIngestQueue(
        doc({ ingest_attempts: 1, ingest_next_retry_at: "昨天" }),
        NOW,
      ),
    ).toBeNull();
  });
});
