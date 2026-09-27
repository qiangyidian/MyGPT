import { describe, expect, it } from "vitest";

import {
  backoffDelayMs,
  jitteredDelayMs,
  POLL_STOPPED_MESSAGE,
  RUN_POLL,
  SLOW_POLL,
  shouldPollNow,
  shouldStopPolling,
} from "../poll-policy";

describe("backoffDelayMs", () => {
  it("失败越多等得越久，按 2 的幂增长", () => {
    expect(backoffDelayMs(0, RUN_POLL)).toBe(4_000);
    expect(backoffDelayMs(1, RUN_POLL)).toBe(8_000);
    expect(backoffDelayMs(2, RUN_POLL)).toBe(16_000);
  });

  it("再糟也有上限，不会退化成永远不刷新", () => {
    expect(backoffDelayMs(50, RUN_POLL)).toBe(RUN_POLL.ceilingMs);
    expect(backoffDelayMs(1000, SLOW_POLL)).toBe(SLOW_POLL.ceilingMs);
  });

  it("长时间故障下 2^n 溢出也必须给出可用间隔", () => {
    // Math.pow(2, 800) === Infinity；不加保护会把 setTimeout 变成 0 延迟的疯狂重试。
    expect(backoffDelayMs(800, RUN_POLL)).toBe(RUN_POLL.ceilingMs);
  });

  it("负数失败次数按基准值处理", () => {
    expect(backoffDelayMs(-3, RUN_POLL)).toBe(RUN_POLL.baseMs);
  });
});

describe("jitteredDelayMs", () => {
  it("在 [delay, delay*1.2] 内取值，避免整排客户端同一毫秒重试", () => {
    expect(jitteredDelayMs(1000, () => 0)).toBe(1000);
    expect(jitteredDelayMs(1000, () => 1)).toBe(1200);
    expect(jitteredDelayMs(1000, () => 0.5)).toBe(1100);
  });

  it("rand 越界会被夹住", () => {
    expect(jitteredDelayMs(1000, () => -5)).toBe(1000);
    expect(jitteredDelayMs(1000, () => 9)).toBe(1200);
  });
});

describe("shouldStopPolling", () => {
  it("到达连续失败上限就停", () => {
    expect(shouldStopPolling(RUN_POLL.maxConsecutiveFailures - 1, RUN_POLL)).toBe(false);
    expect(shouldStopPolling(RUN_POLL.maxConsecutiveFailures, RUN_POLL)).toBe(true);
  });

  it("上限为 0 表示只限速、永不放弃", () => {
    expect(shouldStopPolling(999, { ...RUN_POLL, maxConsecutiveFailures: 0 })).toBe(false);
  });
});

describe("shouldPollNow", () => {
  it("后台标签页与被放弃的轮询都不发请求", () => {
    expect(shouldPollNow(true, { wasStopped: false })).toBe(true);
    expect(shouldPollNow(false, { wasStopped: false })).toBe(false);
    expect(shouldPollNow(true, { wasStopped: true })).toBe(false);
  });
});

describe("POLL_STOPPED_MESSAGE", () => {
  it("放弃自动刷新时有中文说明可给界面用", () => {
    expect(POLL_STOPPED_MESSAGE).toContain("已暂停自动刷新");
  });
});
