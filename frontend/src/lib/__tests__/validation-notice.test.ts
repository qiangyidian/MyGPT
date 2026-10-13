import { describe, it, expect } from "vitest";

import {
  checkLabel,
  failedChecks,
  noticeFromMetadata,
  validationBadge,
} from "@/lib/validation-notice";
import type { ValidationCheck, ValidationNotice } from "@/lib/types";

// 这里钉的是「用户会不会看到提示、看到的是哪一句」。实时帧与刷新后的 metadata 是
// **两条路**，角标必须两条都出得来 —— 只在实时出现的话，用户刷新后再来问「刚才那句
// 提示是什么意思」就没人答得上。

const notice = (over: Partial<ValidationNotice> = {}): ValidationNotice => ({
  traceId: "tr-1",
  status: "answered",
  action: "return_answer",
  checks: [],
  message: "",
  correctedText: null,
  droppedClaimCount: 0,
  refusalReason: null,
  risk: { is_high_risk: false },
  ...over,
});

const check = (
  name: string,
  status: ValidationCheck["status"]
): ValidationCheck => ({ name, status });

describe("角标：干净的回合不显示", () => {
  it("null 不显示", () => {
    expect(validationBadge(null)).toBeNull();
  });

  it("全部通过（含 skip / audit）不显示 —— 每个回答挂个绿勾等于让人无视它", () => {
    expect(
      validationBadge(
        notice({
          checks: [
            check("schema", "pass"),
            check("authorization_recheck", "skip"),
            check("grounding", "audit"),
          ],
        })
      )
    ).toBeNull();
  });
});

describe("角标：拒答", () => {
  it("refused 是最高一档", () => {
    const badge = validationBadge(
      notice({
        status: "refused",
        action: "refuse",
        refusalReason: "high_risk_grounding",
        checks: [check("high_risk_grounding", "fail")],
      })
    );
    expect(badge?.tone).toBe("danger");
    expect(badge?.label).toContain("拒答");
    expect(badge?.details).toContain("高风险问题的依据");
  });

  it("insufficient_evidence 说的是「没找到依据」，不是「拒答」", () => {
    const badge = validationBadge(
      notice({ status: "insufficient_evidence", action: "refuse" })
    );
    expect(badge?.tone).toBe("danger");
    expect(badge?.label).toContain("知识库");
  });
});

describe("角标：降级", () => {
  it("摘除主张时给出条数", () => {
    const badge = validationBadge(
      notice({
        action: "drop_claims",
        droppedClaimCount: 2,
        correctedText: "剩下的。",
        checks: [check("citation_ids_exist", "fail")],
      })
    );
    expect(badge?.tone).toBe("warning");
    expect(badge?.label).toContain("2");
    expect(badge?.details).toContain("引用来源存在性");
  });

  it("有检查没过但状态说没事时，不静默丢弃", () => {
    const badge = validationBadge(notice({ checks: [check("length", "fail")] }));
    expect(badge?.tone).toBe("warning");
    expect(badge?.details).toContain("长度限制");
  });
});

describe("刷新后的 metadata → 同一份结论", () => {
  const metadata = {
    trace_id: "tr-9",
    answer_status: "refused",
    risk: { is_high_risk: true, categories: ["permission"] },
    validation: {
      policy_version: "policy-v1",
      status: "refused",
      action: "refuse",
      checks: [check("high_risk_grounding", "fail")],
      dropped_claim_count: 0,
      refusal_reason: "high_risk_grounding",
    },
  };

  it("能还原出结论与角标", () => {
    const n = noticeFromMetadata(metadata);
    expect(n?.traceId).toBe("tr-9");
    expect(n?.status).toBe("refused");
    expect(n?.risk.is_high_risk).toBe(true);
    expect(validationBadge(n)?.tone).toBe("danger");
  });

  it("没有 validation 块的旧消息返回 null，不猜", () => {
    expect(noticeFromMetadata({})).toBeNull();
    expect(noticeFromMetadata(undefined)).toBeNull();
    expect(noticeFromMetadata({ trace_id: "tr-1" })).toBeNull();
  });

  it("metadata 里没有正文文案，角标也不靠它", () => {
    const n = noticeFromMetadata(metadata);
    expect(n?.message).toBe("");
    expect(n?.correctedText).toBeNull();
    expect(validationBadge(n)?.label).toBeTruthy();
  });
});

describe("检查项名字", () => {
  it("认识的翻译成中文", () => {
    expect(checkLabel("pii_policy")).toBe("敏感信息");
  });

  it("不认识的原样返回，不吞掉", () => {
    expect(checkLabel("brand_new_check")).toBe("brand_new_check");
  });

  it("只挑 fail", () => {
    expect(
      failedChecks([
        check("a", "pass"),
        check("b", "fail"),
        check("c", "skip"),
        check("d", "audit"),
      ]).map((c) => c.name)
    ).toEqual(["b"]);
  });
});
