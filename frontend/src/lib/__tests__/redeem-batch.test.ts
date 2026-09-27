// 兑换码批次的纯逻辑（管理端运营页用）。
//
// 这些判定决定「一张码发出去多少分」「哪批还能作废」，写错的后果是真金白银，
// 而它们全都与 DOM 无关，所以留在 lib 层测（vitest 是 `environment: "node"`）。
import { describe, expect, it } from "vitest";

import {
  EMPTY_REDEEM_BATCH_FORM,
  REDEEM_BATCH_FILTERS,
  REDEEM_BATCH_LIMITS,
  buildRedeemCodesCsv,
  formatRedeemBatchPreview,
  hasRedeemBatchFormErrors,
  isBatchExpired,
  parseBackendInstant,
  redeemBatchQuery,
  redeemBatchRequest,
  redeemBatchStatus,
  validateRedeemBatchForm,
  voidBatchConfirmed,
  voidBatchConsequences,
  type RedeemBatchForm,
} from "@/lib/redeem-batch";
import type { RedeemBatchProgress } from "@/lib/types";

/** 注入的「现在」：所有时间相关规则都要能被钉住，否则测试会随机器时钟漂移。 */
const NOW = new Date(Date.UTC(2026, 8, 20, 12, 0, 0));

function form(overrides: Partial<RedeemBatchForm> = {}): RedeemBatchForm {
  return { ...EMPTY_REDEEM_BATCH_FORM, ...overrides };
}

function row(
  counts: Partial<Pick<RedeemBatchProgress, "total" | "redeemed" | "void" | "active">> = {},
  batch: Partial<RedeemBatchProgress["batch"]> = {}
): RedeemBatchProgress {
  return {
    batch: {
      id: "b1",
      name: "2026 中秋活动",
      credits_per_code: 1000,
      expires_at: null,
      note: null,
      created_at: "2026-09-01T00:00:00Z",
      ...batch,
    },
    total: 0,
    redeemed: 0,
    void: 0,
    active: 0,
    ...counts,
  };
}

describe("validateRedeemBatchForm", () => {
  it("默认表单可以提交", () => {
    expect(validateRedeemBatchForm(form({ name: "中秋" }), NOW)).toEqual({});
    expect(hasRedeemBatchFormErrors({})).toBe(false);
  });

  it("批次名去空白后必填，且有长度上限", () => {
    expect(validateRedeemBatchForm(form({ name: "   " }), NOW).name).toBe("批次名称不能为空");
    expect(
      validateRedeemBatchForm(form({ name: "x".repeat(REDEEM_BATCH_LIMITS.nameMax + 1) }), NOW)
        .name
    ).toContain("不能超过");
  });

  it("张数与积分必须是整数：负数报「太小」，小数报「不是整数」", () => {
    const errors = validateRedeemBatchForm(form({ count: "-5", credits_per_code: "1.5" }), NOW);
    expect(errors.count).toContain("不能小于 1");
    expect(errors.credits_per_code).toContain("不带小数点");
    expect(validateRedeemBatchForm(form({ count: "" }), NOW).count).toContain("不能为空");
  });

  it("张数封顶在后端的单批上限", () => {
    expect(
      validateRedeemBatchForm(form({ count: String(REDEEM_BATCH_LIMITS.countMax + 1) }), NOW).count
    ).toContain(`不能超过 ${REDEEM_BATCH_LIMITS.countMax}`);
  });

  it("有效期留空是永久，填了就必须晚于现在", () => {
    expect(validateRedeemBatchForm(form({ expires_at: "" }), NOW).expires_at).toBeUndefined();
    expect(validateRedeemBatchForm(form({ expires_at: "2020-01-01" }), NOW).expires_at).toBe(
      "有效期必须晚于当前时间"
    );
    expect(validateRedeemBatchForm(form({ expires_at: "2099-12-31" }), NOW).expires_at).toBeUndefined();
    expect(validateRedeemBatchForm(form({ expires_at: "not-a-date" }), NOW).expires_at).toBe(
      "有效期格式不正确"
    );
  });
});

describe("redeemBatchRequest / 预览", () => {
  it("清洗成请求体：留空字段发 null 而不是空串", () => {
    expect(
      redeemBatchRequest(form({ name: "  中秋  ", note: "  ", expires_at: "" }))
    ).toMatchObject({
      name: "中秋",
      credits_per_code: 1000,
      count: 10,
      expires_at: null,
      note: null,
    });
  });

  it("合计写清楚「几张 × 每张多少」，字段非法时不给假数字", () => {
    expect(formatRedeemBatchPreview(form({ credits_per_code: "200", count: "3" }))).toContain(
      "3 张 × 每张 200"
    );
    expect(formatRedeemBatchPreview(form({ count: "0" }))).toBeNull();
    expect(formatRedeemBatchPreview(form({ count: "99999" }))).toBeNull();
    expect(formatRedeemBatchPreview(form({ credits_per_code: "abc" }))).toBeNull();
  });
});

describe("parseBackendInstant / isBatchExpired", () => {
  it("没有时区后缀的时间串按 UTC 读，两套数据库才给出同一个「是否过期」", () => {
    expect(parseBackendInstant("2026-09-01T00:00:00")).toBe(Date.UTC(2026, 8, 1) / 1000);
    expect(parseBackendInstant("2026-09-01T00:00:00Z")).toBe(Date.UTC(2026, 8, 1) / 1000);
    expect(parseBackendInstant(null)).toBeNull();
    expect(parseBackendInstant("   ")).toBeNull();
    expect(parseBackendInstant("胡说八道")).toBeNull();
  });

  it("永久有效（null）永不过期", () => {
    expect(isBatchExpired(null, NOW)).toBe(false);
    expect(isBatchExpired("2020-01-01T00:00:00Z", NOW)).toBe(true);
    expect(isBatchExpired("2099-01-01T00:00:00Z", NOW)).toBe(false);
  });
});

describe("redeemBatchStatus", () => {
  it("还有未兑换码：未过期算兑换中，过期算已过期，两者都能继续作废", () => {
    const live = redeemBatchStatus(row({ total: 10, active: 4 }), NOW);
    expect(live).toMatchObject({ key: "active", voidable: true });
    const dead = redeemBatchStatus(
      row({ total: 10, active: 4 }, { expires_at: "2020-01-01T00:00:00Z" }),
      NOW
    );
    expect(dead).toMatchObject({ key: "expired", voidable: true });
  });

  it("没有未兑换码时不再给「作废剩余」入口", () => {
    expect(redeemBatchStatus(row({ total: 10, redeemed: 10 }), NOW).key).toBe("used_up");
    expect(redeemBatchStatus(row({ total: 10, void: 10 }), NOW).key).toBe("all_void");
    expect(redeemBatchStatus(row({ total: 10, redeemed: 6, void: 4 }), NOW).key).toBe("no_active");
    for (const key of ["used_up", "all_void", "no_active"] as const) {
      const sample =
        key === "used_up"
          ? row({ total: 10, redeemed: 10 })
          : key === "all_void"
            ? row({ total: 10, void: 10 })
            : row({ total: 10, redeemed: 6, void: 4 });
      expect(redeemBatchStatus(sample, NOW)).toMatchObject({ key, voidable: false });
    }
  });

  it("脏数据（计数缺失或 0 张）不会算出除零或假状态", () => {
    expect(redeemBatchStatus(row({}), NOW).key).toBe("empty");
    // SQLite 可能把计数回成字符串。
    expect(
      redeemBatchStatus(
        { ...row({}), total: "10" as unknown as number, active: "3" as unknown as number },
        NOW
      ).key
    ).toBe("active");
  });
});

describe("redeemBatchQuery", () => {
  it("翻页 = limit + 递增的 offset", () => {
    expect(redeemBatchQuery("all", "", 0, 50)).toEqual({ limit: 50, offset: 0 });
    expect(redeemBatchQuery("all", "", 2, 50)).toEqual({ limit: 50, offset: 100 });
    // 负页码会拼出 `offset=-50`，后端 422；这里钳住。
    expect(redeemBatchQuery("settled", "", -3, 50).offset).toBe(0);
  });

  it("空搜索与 all 不发送：它们是后端的默认值，写进 URL 只是噪音", () => {
    expect(redeemBatchQuery("all", "   ", 1, 50)).toEqual({ limit: 50, offset: 50 });
    expect(redeemBatchQuery("expired", "  中秋  ", 0, 50)).toEqual({
      limit: 50,
      offset: 0,
      search: "中秋",
      status: "expired",
    });
  });

  it("筛选项与后端 BatchStatusFilter 一字不差（分页后筛选只能由 SQL 做）", () => {
    // 「还有未兑换码」= active > 0，含已过期但仍有未兑换码的批次；过期批另有
    // 「已过期」这一栏看，两种口径在后端是两条独立谓词，前端不再自己发明第三种。
    expect(REDEEM_BATCH_FILTERS.map((f) => f.value)).toEqual([
      "all",
      "operable",
      "expired",
      "settled",
    ]);
  });
});

describe("作废整批的确认", () => {
  it("后果清单必须说明不可恢复，并如实带上服务端计数", () => {
    // 刻意选个不到千位的乘积：`formatCreditsRaw` 的千分位分隔符不该成为断言的一部分。
    const consequences = voidBatchConsequences(
      row({ total: 10, redeemed: 3, void: 1, active: 6 }, { credits_per_code: 300 }),
      NOW
    );
    expect(consequences.confirmation).toBe("2026 中秋活动");
    const text = consequences.lines.join("\n");
    expect(text).toContain("剩余的 6 张");
    expect(text).toContain("已兑换的 3 张不受影响");
    expect(text).toContain("900");
    expect(text).toContain("已作废 1 张");
    expect(text).toContain("后端不提供反作废接口");
  });

  it("已过期的批次要多解释一句，因为「作废」对它其实只是把状态定死", () => {
    const plain = voidBatchConsequences(row({ total: 5, active: 5 }), NOW).lines.length;
    const expired = voidBatchConsequences(
      row({ total: 5, active: 5 }, { expires_at: "2020-01-01T00:00:00Z" }),
      NOW
    ).lines.length;
    expect(expired).toBe(plain + 1);
  });

  it("确认词要逐字对上批次名（首尾空白不算数）", () => {
    expect(voidBatchConfirmed("中秋活动", "  中秋活动 ")).toBe(true);
    expect(voidBatchConfirmed("中秋活动", "中秋")).toBe(false);
    expect(voidBatchConfirmed("   ", "")).toBe(false);
  });
});

describe("buildRedeemCodesCsv", () => {
  it("带 BOM，且把批次与有效期写进每一行", () => {
    const csv = buildRedeemCodesCsv(["AAAA", "BBBB"], {
      batchName: "中秋",
      creditsPerCode: 1000,
      expiresText: "永久",
    });
    expect(csv.startsWith("﻿")).toBe(true);
    const lines = csv.slice(1).split("\n");
    expect(lines[0]).toBe("兑换码,批次,每码积分,有效期");
    expect(lines[1]).toBe("AAAA,中秋,1000,永久");
    expect(lines).toHaveLength(3);
  });

  it("含逗号或引号的批次名要按 CSV 规则转义，否则导出会串列", () => {
    const csv = buildRedeemCodesCsv(["AAAA"], {
      batchName: '中秋, "内部"',
      creditsPerCode: 1,
      expiresText: "永久",
    });
    expect(csv).toContain('"中秋, ""内部"""');
  });
});
