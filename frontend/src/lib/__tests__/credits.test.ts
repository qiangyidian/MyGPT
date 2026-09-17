import { describe, expect, it } from "vitest";

import {
  REDEEM_ERROR_MESSAGES,
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
