import { describe, expect, it } from "vitest";

import {
  REDEEM_ERROR_MESSAGES,
  expiryFromDateInput,
  formatCredits,
  formatCreditsRaw,
  normalizeRedeemCodeInput,
  redeemErrorMessage,
} from "@/lib/credits";

describe("normalizeRedeemCodeInput", () => {
  it("大写并把字母数字分组为四组四位", () => {
    expect(normalizeRedeemCodeInput("ab12cd34ef56gh78")).toBe("AB12-CD34-EF56-GH78");
  });

  it("丢弃用户粘贴进来的分隔符与空白", () => {
    expect(normalizeRedeemCodeInput(" ab12 cd34-ef56_gh78 ")).toBe("AB12-CD34-EF56-GH78");
  });

  it("修正手抄歧义字符 I/L→1、O→0", () => {
    expect(normalizeRedeemCodeInput("oooo1111lllloo11")).toBe("0000-1111-1111-0011");
  });

  it("超过 16 位时截断，不无限增长", () => {
    const out = normalizeRedeemCodeInput("a".repeat(40));
    expect(out.replace(/-/g, "")).toHaveLength(16);
  });

  it("不足一组时不补分隔符", () => {
    expect(normalizeRedeemCodeInput("ab1")).toBe("AB1");
  });

  it("空输入返回空", () => {
    expect(normalizeRedeemCodeInput("")).toBe("");
    expect(normalizeRedeemCodeInput("   ")).toBe("");
  });

  it("全角字母数字折叠回 ASCII（与后端 normalize_code 一致）", () => {
    // 全角码必须被折叠而不是被 [A-Z0-9] 丢弃 —— 否则粘贴全角码会在
    // 16 位长度校验上卡住，用户永远提交不出去。
    const wide = "ＡＢ１２ＣＤ３４ＥＦ５６ＧＨ７８";
    expect(normalizeRedeemCodeInput(wide)).toBe("AB12-CD34-EF56-GH78");
    expect(normalizeRedeemCodeInput(wide)).toBe(
      normalizeRedeemCodeInput("ab12cd34ef56gh78")
    );
  });

  it("全角字符不会被静默丢弃（回归：曾因过滤顺序而截断）", () => {
    const out = normalizeRedeemCodeInput("ＡＢ１２");
    expect(out.replace(/-/g, "")).toHaveLength(4);
    expect(out).toBe("AB12");
  });
});

describe("formatCredits", () => {
  it("负余额显示为 0（最后一轮透支在用户侧不应显示成欠款）", () => {
    expect(formatCredits(-15)).toBe("0");
    expect(formatCredits(-1)).toBe("0");
  });

  it("null / undefined 显示为 0", () => {
    expect(formatCredits(null)).toBe("0");
    expect(formatCredits(undefined)).toBe("0");
  });

  it("正数带千分位", () => {
    expect(formatCredits(0)).toBe("0");
    expect(formatCredits(1234)).toBe("1,234");
    expect(formatCredits(1000000)).toBe("1,000,000");
  });
});

describe("formatCreditsRaw", () => {
  it("负数原样保留（管理侧负余额是超扣信号，不能被钳位成 0）", () => {
    expect(formatCreditsRaw(-300)).toBe("-300");
  });

  it("与 formatCredits 对同一输入行为相反，防止两者被静默合并", () => {
    expect(formatCredits(-300)).toBe("0");
    expect(formatCreditsRaw(-300)).toBe("-300");
  });

  it("正数带千分位、null / undefined 显示为 0（与 formatCredits 一致）", () => {
    expect(formatCreditsRaw(1234)).toBe("1,234");
    expect(formatCreditsRaw(null)).toBe("0");
    expect(formatCreditsRaw(undefined)).toBe("0");
  });
});

describe("redeemErrorMessage", () => {
  it("每个后端错误码都有中文文案", () => {
    for (const code of [
      "redeem_code_not_found",
      "redeem_code_used",
      "redeem_code_expired",
      "redeem_code_void",
    ]) {
      expect(REDEEM_ERROR_MESSAGES[code]).toBeTruthy();
    }
  });

  it("未知码回落到后端给的 message", () => {
    expect(redeemErrorMessage("something_new", "后端说的一句话")).toBe("后端说的一句话");
  });

  it("未知码且没有 message 时给通用文案", () => {
    expect(redeemErrorMessage("something_new", "")).toBe("兑换失败，请稍后重试");
  });

  it("已知码优先用映射文案而不是后端 message", () => {
    expect(redeemErrorMessage("redeem_code_used", "raw")).toContain("已被使用");
  });
});

describe("expiryFromDateInput", () => {
  it("返回所选日期本地时间的最后一刻，而不是 UTC 午夜", () => {
    const iso = expiryFromDateInput("2026-09-30")!;
    expect(iso).not.toBeNull();
    const d = new Date(iso);
    // 结束在本地 23:59:59.999 —— 用本地字段断言，不依赖运行时区。
    expect(d.getFullYear()).toBe(2026);
    expect(d.getMonth()).toBe(8);
    expect(d.getDate()).toBe(30);
    expect(d.getHours()).toBe(23);
    expect(d.getMinutes()).toBe(59);
    expect(d.getSeconds()).toBe(59);
    expect(d.getMilliseconds()).toBe(999);
  });

  it("日期串绝不会被当成 UTC 午夜解析（new Date('2026-09-30') 的陷阱）", () => {
    // new Date("2026-09-30") 是 2026-09-30T00:00:00Z = 北京时间 08:00。
    // helper 的结果必须不等于那个 UTC 午夜。
    const utcMidnight = new Date("2026-09-30").toISOString(); // "2026-09-30T00:00:00.000Z"
    expect(expiryFromDateInput("2026-09-30")).not.toBe(utcMidnight);
  });

  it("结果是本地午夜起一整天减一毫秒", () => {
    const end = new Date(expiryFromDateInput("2026-09-30")!).getTime();
    const localMidnight = new Date(2026, 8, 30).getTime();
    expect(end - localMidnight).toBe(24 * 3600_000 - 1);
  });

  it("非 UTC 时区下，结果晚于当日 UTC 午夜 16 小时（北京时间场景）", () => {
    // 北京时间运行时的核心场景：new Date("2026-09-30") 是北京时间 08:00，
    // 而我们的结果应晚它近一整天 —— 这正是"提前 8 小时过期"陷阱的反向断言。
    const utcMidnight = new Date("2026-09-30").getTime();
    const localMidnight = new Date(2026, 8, 30).getTime();
    const offset = localMidnight - utcMidnight; // 该日本地午夜相对 UTC 午夜的偏移
    if (offset === 0) return; // UTC 运行环境下两指相同，无差异可断言
    const end = new Date(expiryFromDateInput("2026-09-30")!).getTime();
    expect(end - utcMidnight).toBe(offset + 24 * 3600_000 - 1);
  });

  it("空输入返回 null（永久有效）", () => {
    expect(expiryFromDateInput("")).toBeNull();
    expect(expiryFromDateInput("   ")).toBeNull();
  });
});
