import { describe, it, expect } from "vitest";

import {
  PASSWORD_POLICY,
  passwordPolicySummary,
  passwordProblem,
} from "@/lib/password-policy";

// 这些数字与文案不是前端的选择，是后端 `app/core/security.py` + `schemas/auth.py`
// 的抄本（逐条出处见 `password-policy.ts` 顶部）。抄本会漂，所以在这里钉死：
// 后端改常量时这条测试先响，而不是等用户提交后才撞 422。

describe("常量与后端同源", () => {
  it("最短 8 位（config.py:621 + schemas/auth.py 的 min_length=8）", () => {
    expect(PASSWORD_POLICY.minLength).toBe(8);
  });

  it("最长 128 位（schemas/auth.py:17,41,42,56 的 max_length=128）", () => {
    expect(PASSWORD_POLICY.maxLength).toBe(128);
  });

  it("要求大写+小写+数字（config.py:622 的 PASSWORD_REQUIRE_COMPLEXITY 默认 True）", () => {
    expect(PASSWORD_POLICY.requireComplexity).toBe(true);
  });
});

describe("长度规则", () => {
  it("7 位挡下，8 位放行（后端也是 <8 才抛）", () => {
    expect(passwordProblem("Abcdef1")).toBe("密码至少需要 8 个字符");
    expect(passwordProblem("Abcdefg1")).toBeNull();
  });

  it("文案逐字等于后端 security.py:62 的那句，不自己改写", () => {
    expect(passwordProblem("")).toBe("密码至少需要 8 个字符");
    expect(passwordProblem("   ")).toBe("密码至少需要 8 个字符");
  });

  it("128 位接受、129 位挡下（前端提前说，免得撞 422）", () => {
    const base = "Abcdefg1";
    expect(passwordProblem(base + "x".repeat(PASSWORD_POLICY.maxLength - base.length))).toBeNull();
    expect(passwordProblem(base + "x".repeat(PASSWORD_POLICY.maxLength - base.length + 1))).toBe(
      `密码最多 ${PASSWORD_POLICY.maxLength} 个字符`
    );
  });

  it("按码点计数：一个 emoji 算 1 位，不会因 UTF-16 代理对被判成 2 位", () => {
    // 7 个 emoji 的 .length 是 14，但后端 len() 看到的是 7 —— 必须挡下。
    const seven = String.fromCodePoint(0x1f600).repeat(7);
    expect(seven.length).toBe(14);
    expect(passwordProblem(seven)).toBe("密码至少需要 8 个字符");
  });
});

describe("复杂度规则", () => {
  it("缺任意一类都挡下，并且只说同一句后端文案", () => {
    const cases = [
      "abcdefgh", // 没大写没数字
      "ABCDEFGH", // 没小写没数字
      "12345678", // 没字母
      "Abcdefgh", // 没数字
      "abcdefg1", // 没大写
      "ABCDEFG1", // 没小写
    ];
    for (const pwd of cases) {
      expect(passwordProblem(pwd)).toBe("密码需包含大写字母、小写字母和数字");
    }
  });

  it("三类齐全即通过：符号、空格、非 ASCII 字母都不额外要求", () => {
    expect(passwordProblem("Ab1cdefg")).toBeNull();
    expect(passwordProblem("Passw0rd!@#")).toBeNull();
    expect(passwordProblem("密码 Pass1")).toBeNull();
  });

  it("现状：只认 ASCII 的大小写（后端用 Python 的 islower/isupper，认全字母）", () => {
    // 「Пароль1м」有全字母的大小写和数字，后端收；这里按 ASCII 判，挡下。
    expect(passwordProblem("Пароль1м")).toBe("密码需包含大写字母、小写字母和数字");
  });

  it("顺序先长度后复杂度：太短时报的是长度那条", () => {
    expect(passwordProblem("abc")).toBe("密码至少需要 8 个字符");
  });
});

describe("提示文案", () => {
  it("一行摘要同时给出长度与三类字符，供 placeholder 与说明共用", () => {
    const summary = passwordPolicySummary();
    expect(summary).toContain(String(PASSWORD_POLICY.minLength));
    expect(summary).toContain("大写字母");
    expect(summary).toContain("小写字母");
    expect(summary).toContain("数字");
  });

  it("摘要里的长度与挡下短密码时说的长度一致（两处不能各说一个数）", () => {
    const summary = passwordPolicySummary();
    const tooShort = passwordProblem("Ab1cdef") as string;
    expect(summary).toContain(String(PASSWORD_POLICY.minLength));
    expect(tooShort).toContain(String(PASSWORD_POLICY.minLength));
  });
});
